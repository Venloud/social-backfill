"""Safe regression coverage for two TikTok inputs sharing one publishing queue."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline import manifest as mf, tiktok_auth
from pipeline.publish_one import discover_readonly, pick_jobs


def video(vid, created=1):
    return {"id": str(vid), "create_time": created,
            "share_url": f"https://www.tiktok.com/@example/video/{vid}",
            "title": "Test video"}


class TwoSourceTests(unittest.TestCase):
    def test_source_attribution_and_legacy_migration(self):
        m = mf.load(Path("/tmp/nonexistent-social-backfill-test-manifest"))
        mf.merge(m, [video("101")])
        self.assertEqual(m["videos"]["101"]["source"]["account"], "primary")
        m["videos"]["101"]["source"].pop("account")
        m["videos"]["101"]["destinations"]["youtube"]["status"] = "published"
        mf.merge(m, [video("101")], source_account="primary")
        self.assertEqual(m["videos"]["101"]["source"]["account"], "primary")
        self.assertEqual(m["videos"]["101"]["destinations"]["youtube"]["status"], "published")
        mf.merge(m, [video("202")], source_account="secondary")
        self.assertEqual(m["videos"]["202"]["source"]["account"], "secondary")
        with self.assertRaises(ValueError):
            mf.merge(m, [video("101")], source_account="secondary")

    def test_discovery_combines_both_without_overwriting_publish_state(self):
        m = {"videos": {}}
        with patch.dict(os.environ, {"TIKTOK_ACCESS_TOKEN": "one", "TIKTOK_ACCESS_TOKEN_2": "two"}), \
             patch("pipeline.tiktok_discovery.list_videos", side_effect=[[video("101")], [video("202")]]) as listing:
            discover_readonly(m)
        self.assertEqual(listing.call_count, 2)
        self.assertEqual(set(m["videos"]), {"101", "202"})
        self.assertEqual(m["videos"]["101"]["source"]["account"], "primary")
        self.assertEqual(m["videos"]["202"]["source"]["account"], "secondary")
        self.assertTrue(m["videos"]["202"]["source"]["fresh_priority"])

    def test_one_source_failure_does_not_block_other(self):
        m = {"videos": {}}
        with patch.dict(os.environ, {"TIKTOK_ACCESS_TOKEN": "one", "TIKTOK_ACCESS_TOKEN_2": "two"}), \
             patch("pipeline.tiktok_discovery.list_videos", side_effect=[
                 tiktok_auth.TikTokAuthError("primary unavailable"), [video("202")]]):
            discover_readonly(m)
        self.assertEqual(set(m["videos"]), {"202"})

    def test_shared_queue_rotates_accounts_with_shared_pacing(self):
        m = {"videos": {}}
        mf.merge(m, [video("101", 1)], source_account="primary")
        mf.merge(m, [video("202", 2)], source_account="secondary")
        m["publish_pacing"] = {
            "youtube": {"last_source_account": "primary"},
            "instagram": {"last_source_account": "secondary"},
        }
        jobs = pick_jobs(m)
        self.assertEqual(jobs["youtube"][0], "202")
        self.assertEqual(jobs["instagram"][0], "101")

    def test_secondary_secrets_do_not_overwrite_primary(self):
        with patch.dict(os.environ, {"TIKTOK_ACCOUNT": "secondary",
                                      "GITHUB_REPOSITORY": "Venloud/social-backfill",
                                      "GH_TOKEN": "test"}, clear=True), \
             patch("pipeline.tiktok_auth.subprocess.run") as command:
            command.return_value.returncode = 0
            tiktok_auth.save_secrets({"access_token": "a", "refresh_token": "r", "open_id": "o"})
        saved = [c.args[0][3] for c in command.call_args_list]
        self.assertEqual(saved, ["TIKTOK_REFRESH_TOKEN_2", "TIKTOK_ACCESS_TOKEN_2", "TIKTOK_OPEN_ID_2"])
        with patch.dict(os.environ, {"TIKTOK_ACCOUNT": "primary"}):
            self.assertEqual(tiktok_auth.secret_name("TIKTOK_REFRESH_TOKEN"), "TIKTOK_REFRESH_TOKEN")

    def test_secondary_refresh_exports_separate_env_vars(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "env"
            with patch.dict(os.environ, {"TIKTOK_ACCOUNT": "secondary", "GITHUB_ENV": str(env_file)}), \
                 patch.object(tiktok_auth, "access_token", return_value={
                     "access_token": "secondAccess", "open_id": "secondID",
                     "expires_in": 86400, "refresh_expires_in": 864000}):
                self.assertEqual(tiktok_auth.main(), 0)
            out = env_file.read_text()
            self.assertIn("TIKTOK_ACCESS_TOKEN_2=secondAccess", out)
            self.assertIn("TIKTOK_OPEN_ID_2=secondID", out)
            self.assertNotIn("\nTIKTOK_ACCESS_TOKEN=", out)


if __name__ == "__main__":
    unittest.main()
