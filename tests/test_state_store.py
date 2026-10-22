"""State branch push tests against a real local bare remote, no network."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline import state_store
from pipeline.state_store import StatePushError, StateStore


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


class StateStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.remote = self.tmp / "remote.git"
        git(self.tmp, "init", "-q", "--bare", str(self.remote))
        self.work = self.tmp / "work"
        git(self.tmp, "init", "-q", "-b", "state", str(self.work))
        git(self.work, "config", "user.email", "t@example.com")
        git(self.work, "config", "user.name", "t")
        git(self.work, "remote", "add", "origin", str(self.remote))
        self.store = StateStore(self.work)

    def remote_log(self):
        return subprocess.run(["git", "-C", str(self.remote), "log", "--format=%s", "state"],
                              capture_output=True, text=True).stdout.split("\n")[0]

    def test_push_reaches_remote(self):
        self.store.push({"videos": {}}, "state: first")
        self.assertEqual(self.remote_log(), "state: first")

    def test_transient_push_failure_is_retried(self):
        real = self.store._git
        calls = []

        def flaky(*args):
            if args[0] == "push":
                calls.append(args)
                if len(calls) == 1:
                    return subprocess.CompletedProcess(args, 1, "", "! [remote rejected] HEAD -> state (failed)")
            return real(*args)

        with patch.object(self.store, "_git", flaky), patch.object(state_store.time, "sleep") as sleep:
            self.store.push({"videos": {"a": {}}}, "state: retried")
        self.assertEqual(len(calls), 2)
        sleep.assert_called_once()
        self.assertEqual(self.remote_log(), "state: retried")

    def test_persistent_push_failure_still_blocks_upload(self):
        real = self.store._git

        def broken(*args):
            if args[0] == "push":
                return subprocess.CompletedProcess(args, 1, "", "remote rejected")
            return real(*args)

        with patch.object(self.store, "_git", broken), patch.object(state_store.time, "sleep"):
            with self.assertRaises(StatePushError):
                self.store.push({"videos": {"b": {}}}, "state: never")


if __name__ == "__main__":
    unittest.main()
