"""HTTP, secret and masking helpers shared by every platform module (adapted from kindled_iron/publish.py)."""
from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CONFIG = json.loads((Path(__file__).resolve().parent / "config.json").read_text())
GRAPH_V = CONFIG.get("graph_version", "v23.0")

# Secrets must never be written to these repos from here (other channels' projects).
FORBIDDEN_REPOS = {"venloud/horror-shorts", "venloud/moneyprinterturbo",
                   "venloud/nightfiles", "venloud/kindled-iron-voice"}


def log(msg: str) -> None:
    print(f"[social] {msg}", flush=True)


class Fail(Exception):
    pass


class NotConfigured(Exception):
    """A secret the platform needs is not set: reported as 'not configured', never as a failure."""


def http(url: str, method: str = "GET", data=None, headers: dict | None = None, form: dict | None = None,
         timeout: int = 120) -> dict:
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers = {**(headers or {}), "Content-Type": "application/x-www-form-urlencoded"}
    elif isinstance(data, (dict, list)):
        data = json.dumps(data).encode()
        headers = {**(headers or {}), "Content-Type": "application/json; charset=UTF-8"}
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            out = json.loads(body) if body.strip().startswith(("{", "[")) else {"_body": body[:300]}
            if isinstance(out, dict):
                out["_headers"] = dict(r.headers)
            return out
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            detail = json.loads(body)
            detail = detail.get("error", detail)
        except ValueError:
            detail = body[:300]
        raise Fail(f"HTTP {e.code}: {json.dumps(detail)[:400] if not isinstance(detail, str) else detail}") from None
    except Exception as e:  # noqa: BLE001
        # Never echo the URL: some Graph calls carry a token in the query string.
        raise Fail(f"{type(e).__name__}: {str(e)[:200]}") from None


def configured(*names: str) -> bool:
    return all(os.environ.get(n) for n in names)


def env(*names: str) -> list[str]:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise NotConfigured(f"secret(s) not set: {', '.join(missing)}")
    return [os.environ[n] for n in names]


def mask(value: str) -> None:
    """GitHub Actions hides this value in every later log line (the value itself is never printed otherwise)."""
    if value and os.environ.get("GITHUB_ACTIONS"):
        print(f"::add-mask::{value}", flush=True)


def this_repo() -> str:
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if repo.lower() in FORBIDDEN_REPOS:
        raise Fail(f"refusing to write secrets to {repo}")
    return repo


def save_secret(name: str, value: str) -> bool:
    """Save a refreshed token into THIS repo's Actions secrets only (GH_PAT, value via stdin, never argv).
    Unlike the Kindled Iron original there is no mirroring to other repos."""
    mask(value)
    os.environ[name] = value
    tok, repo = os.environ.get("GH_TOKEN") or os.environ.get("GH_PAT"), this_repo()
    if not tok or not repo:
        log(f"WARNING: {name} refreshed but not saved (no GH_PAT / not in Actions): this run only")
        return False
    r = subprocess.run(["gh", "secret", "set", name, "--repo", repo], input=value,
                       env={**os.environ, "GH_TOKEN": tok}, capture_output=True, text=True)
    if r.returncode:
        log(f"WARNING: could not save {name} to {repo} (GH_PAT needs Secrets: Read and write): "
            f"{r.stderr.strip()[:160]}")
        return False
    log(f"{name} refreshed and saved to {repo}")
    return True
