"""One-time: swap the TikTok login code for tokens (run by the 'Connect TikTok' workflow).

Saves TIKTOK_REFRESH_TOKEN, TIKTOK_ACCESS_TOKEN and TIKTOK_OPEN_ID as secrets of THIS repo.
Then a read-only check: user info and video.list (5 videos).
Prints only PASS/FAIL, the account, the granted scopes, the token lifetimes and video ids.
"""
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tiktok_auth  # noqa: E402


def parse_code(raw: str) -> str:
    raw = raw.strip()
    if "code=" in raw:  # the whole redirect URL was pasted
        query = urllib.parse.urlparse(raw).query or raw.split("?", 1)[-1]
        raw = urllib.parse.parse_qs(query).get("code", [""])[0]  # parse_qs decodes once
    # A bare code copied from the address bar is still percent-encoded (e.g. "*" as %2A).
    # Decode until stable, so a double-encoded paste works too.
    for _ in range(3):
        decoded = urllib.parse.unquote(raw)
        if decoded == raw:
            break
        raw = decoded
    return raw.strip()


def main() -> int:
    code = parse_code(os.environ.get("AUTH_CODE_OR_URL", ""))
    if not code:
        print("FAIL: no code found in the input (paste the full callback URL or just the code)")
        return 1
    tiktok_auth.mask(code)
    try:
        tiktok_auth.check_pat()  # refuse before the one-time code is spent
    except tiktok_auth.TikTokAuthError as e:
        print(f"FAIL: {e}")
        print("The login code was not used; it may still work if you rerun within a few minutes.")
        return 1
    try:
        data = tiktok_auth.exchange_code(code)
    except tiktok_auth.TikTokAuthError as e:
        print(f"FAIL: {e}")
        print("Login codes expire within minutes and work once. Get a fresh one and rerun.")
        return 1

    scopes = tiktok_auth.scopes_of(data)
    print(f"Account: {tiktok_auth.username(data['access_token'])}")
    print(f"Scopes: {', '.join(scopes) or 'none'}")
    print(f"Access token lasts: {tiktok_auth.duration(data.get('expires_in'))}")
    print(f"Refresh token lasts: {tiktok_auth.duration(data.get('refresh_expires_in'))}")
    if tiktok_auth.REQUIRED_SCOPE not in scopes:
        print(f"FAIL: {tiktok_auth.REQUIRED_SCOPE} is not among the granted scopes. Add it to the "
              f"app in the TikTok developer portal, request it in the authorize URL, and "
              f"reconnect. Nothing was saved.")
        return 1
    try:
        tiktok_auth.save_secrets(data)
    except tiktok_auth.TikTokAuthError as e:
        print(f"FAIL: {e}")
        return 1
    print(f"Saved {tiktok_auth.secret_name('TIKTOK_REFRESH_TOKEN')}, "
          f"{tiktok_auth.secret_name('TIKTOK_ACCESS_TOKEN')}, "
          f"{tiktok_auth.secret_name('TIKTOK_OPEN_ID')} "
          f"in {os.environ['GITHUB_REPOSITORY']} ({tiktok_auth.account_slot()})")

    # Read-only check with the new token: video.list for 5 videos (user info was read above).
    try:
        ids = tiktok_auth.video_list(data["access_token"], 5)
    except tiktok_auth.TikTokAuthError as e:
        print(f"FAIL: {e}")
        return 1
    print(f"Videos returned: {len(ids)}")
    for vid in ids:
        print(f"  {vid}")
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
