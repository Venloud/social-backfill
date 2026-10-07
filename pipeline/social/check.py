"""Platform Connection Check: refresh what can be refreshed, check each platform, post NOTHING.

    python -m pipeline.social.check [--out connection_check.json]

Prints PASS / FAIL / not configured per platform with the account name; never a token.
Exit code 1 only if a CONFIGURED platform fails. Facebook and Threads are checked only if their secrets exist.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from pipeline.social import meta, tiktok, youtube
from pipeline.social.common import Fail, NotConfigured, configured


def _tiktok() -> tuple[str, str]:
    c = tiktok.tiktok_check()
    return c["account"], f"scopes {c['scope']}; refresh token valid {c['refresh_expires_days']} days"


def _youtube() -> tuple[str, str]:
    c = youtube.youtube_check()
    return c["account"], f"scopes {c['scope']}"


def _instagram() -> tuple[str, str]:
    c = meta.instagram_check()
    return c["account"], f"{c['login']} login token; refresh: {meta.instagram_refresh(c)}"


def _threads() -> tuple[str, str]:
    c = meta.threads_check()
    return c["account"], f"refresh: {meta.threads_refresh()}"


def _facebook() -> tuple[str, str]:
    c = meta.facebook_check()
    return c["account"], f"page token {meta.facebook_token_status()}"


CHECKS = [("tiktok", _tiktok, True), ("youtube", _youtube, True), ("instagram", _instagram, True),
          ("threads", _threads, configured("THREADS_ACCESS_TOKEN")),
          ("facebook", _facebook, configured("FACEBOOK_PAGE_ID", "FACEBOOK_PAGE_ACCESS_TOKEN"))]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="connection_check.json")
    a = ap.parse_args()
    results, failed = [], False
    for name, fn, wanted in CHECKS:
        if not wanted:
            continue
        r = {"platform": name}
        try:
            r["account"], r["detail"] = fn()
            r["status"] = "PASS"
        except NotConfigured as e:
            r.update(status="not configured", detail=str(e))
        except Fail as e:
            r.update(status="FAIL", detail=str(e)[:300])
            failed = True
        except Exception as e:  # noqa: BLE001 - one platform never stops the others
            r.update(status="FAIL", detail=f"{type(e).__name__}: {str(e)[:200]}")
            failed = True
        results.append(r)
        acct = f" ({r['account']})" if r.get("account") else ""
        print(f"{name}: {r['status']}{acct} - {r['detail']}", flush=True)
    Path(a.out).write_text(json.dumps(results, indent=1))
    print("Connection check: " + " / ".join(f"{r['platform']} {r['status']}" for r in results))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
