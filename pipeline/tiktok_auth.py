"""TikTok OAuth v2 helpers for social-backfill.

TikTok access tokens last 24 h and a refresh can hand back a NEW refresh token, so every
TikTok API call must start with `access_token()`: it refreshes, masks the tokens in the
Actions log, and saves the new TIKTOK_ACCESS_TOKEN / TIKTOK_REFRESH_TOKEN / TIKTOK_OPEN_ID
back into THIS repo's secrets (needs GH_TOKEN = a PAT with Secrets: read and write here).

As a workflow step, `python pipeline/tiktok_auth.py` refreshes and exports the fresh token
as TIKTOK_ACCESS_TOKEN / TIKTOK_OPEN_ID for the job's later steps (via $GITHUB_ENV).
Run TikTok jobs in the `tiktok-token` concurrency group so two refreshes never race.
"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
USER_INFO_URL = "https://open.tiktokapis.com/v2/user/info/"
REQUIRED_SCOPE = "video.list"
FORBIDDEN_REPOS = {"venloud/horror-shorts"}


class TikTokAuthError(RuntimeError):
    pass


def mask(value) -> None:
    """Hide a secret value in every later line of the GitHub Actions log."""
    if value and os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::add-mask::{value}", flush=True)


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise TikTokAuthError(f"missing secret {name}")
    return value


def _request(url: str, data: dict = None, headers: dict = None) -> dict:
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode() or "{}")
        except ValueError:
            return {"error": f"http_{e.code}"}


def _token_call(fields: dict) -> dict:
    """POST to TikTok's v2 token endpoint; mask every token in the answer before anything prints."""
    data = _request(TOKEN_URL, {"client_key": _env("TIKTOK_CLIENT_KEY"),
                                "client_secret": _env("TIKTOK_CLIENT_SECRET"), **fields},
                    {"Content-Type": "application/x-www-form-urlencoded",
                     "Cache-Control": "no-cache"})
    for key in ("access_token", "refresh_token", "open_id"):
        mask(data.get(key))
    if not data.get("access_token") or not data.get("refresh_token"):
        # Error answers carry no tokens; only the error code + description are shown.
        raise TikTokAuthError(f"{data.get('error') or 'no token returned'}: "
                              f"{data.get('error_description', '')}".strip(": "))
    return data


def scopes_of(data: dict) -> list:
    return sorted(s.strip() for s in str(data.get("scope", "")).split(",") if s.strip())


def exchange_code(code: str) -> dict:
    return _token_call({"code": code, "grant_type": "authorization_code",
                        "redirect_uri": _env("TIKTOK_REDIRECT_URI")})


def refresh() -> dict:
    old = _env("TIKTOK_REFRESH_TOKEN")
    mask(old)
    return _token_call({"grant_type": "refresh_token", "refresh_token": old})


def target_repo() -> str:
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not repo:
        raise TikTokAuthError("GITHUB_REPOSITORY not set (secrets are only saved from Actions)")
    if repo.lower() in FORBIDDEN_REPOS:
        raise TikTokAuthError(f"refusing to write secrets to {repo}")
    return repo


def save_secrets(data: dict) -> None:
    """Write the three TikTok secrets into this repo only (values go via stdin, never argv)."""
    repo = target_repo()
    if not os.environ.get("GH_TOKEN"):
        raise TikTokAuthError("GH_TOKEN (the GH_PAT secret) is missing; cannot save secrets")
    values = {"TIKTOK_REFRESH_TOKEN": data["refresh_token"],
              "TIKTOK_ACCESS_TOKEN": data["access_token"],
              "TIKTOK_OPEN_ID": data.get("open_id", "")}
    for name, value in values.items():
        if not value:
            raise TikTokAuthError(f"TikTok returned no value for {name}")
        res = subprocess.run(["gh", "secret", "set", name, "--repo", repo],
                             input=value, text=True, capture_output=True)
        if res.returncode != 0:
            raise TikTokAuthError(f"could not save {name} in {repo} "
                                  f"(check GH_PAT has Secrets: read and write): "
                                  f"{res.stderr.strip()[:300]}")


def username(access_token: str) -> str:
    """Read-only profile lookup; username needs user.info.profile, display_name user.info.basic."""
    for fields in ("username,display_name", "display_name"):
        data = _request(f"{USER_INFO_URL}?fields={fields}",
                        headers={"Authorization": f"Bearer {access_token}"})
        user = (data.get("data") or {}).get("user") or {}
        if user.get("username"):
            return "@" + user["username"]
        if user.get("display_name"):
            return user["display_name"] + " (display name; username needs user.info.profile)"
    return "unknown (user.info scopes not granted)"


def duration(seconds) -> str:
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return "unknown"
    if s >= 86400:
        return f"{s / 86400:.1f} days ({s} s)"
    return f"{s / 3600:.1f} hours ({s} s)"


def access_token(save: bool = True) -> dict:
    """Call before EVERY TikTok API call: refresh, save the new tokens back, return the answer."""
    data = refresh()
    if REQUIRED_SCOPE not in scopes_of(data):
        raise TikTokAuthError(f"{REQUIRED_SCOPE} is not among the granted scopes "
                              f"({', '.join(scopes_of(data)) or 'none'}); reconnect TikTok")
    if save:
        save_secrets(data)
    return data


def main() -> int:
    try:
        data = access_token()
    except TikTokAuthError as e:
        print(f"FAIL: TikTok refresh: {e}")
        return 1
    env_file = os.environ.get("GITHUB_ENV")
    if env_file:
        with open(env_file, "a") as f:
            f.write(f"TIKTOK_ACCESS_TOKEN={data['access_token']}\n"
                    f"TIKTOK_OPEN_ID={data.get('open_id', '')}\n")
    print(f"PASS: TikTok token refreshed and saved; access token lasts "
          f"{duration(data.get('expires_in'))}, refresh token "
          f"{duration(data.get('refresh_expires_in'))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
