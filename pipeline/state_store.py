"""Manifest persistence on the dedicated `state` branch (checked out as a git worktree by the workflow).

Every push is a hard requirement before an upload: if the state can't be pushed, nothing is uploaded.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from pipeline import manifest as mf


class StatePushError(RuntimeError):
    pass


class StateStore:
    def __init__(self, worktree: Path):
        self.worktree = worktree
        self.path = worktree / "state" / "manifest.json"

    def load(self) -> dict:
        return mf.load(self.path)

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(self.worktree), *args], capture_output=True, text=True)

    def push(self, manifest: dict, message: str) -> None:
        mf.save(manifest, self.path)
        self._git("add", "state/manifest.json")
        if self._git("diff", "--cached", "--quiet").returncode == 0:
            return
        c = self._git("commit", "-q", "-m", message)
        if c.returncode:
            raise StatePushError(f"git commit failed: {c.stderr.strip()[:200]}")
        p = self._git("push", "-q", "origin", "HEAD:state")
        if p.returncode:
            raise StatePushError(f"git push to the state branch failed: {p.stderr.strip()[:200]}")
