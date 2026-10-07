# Social Backfill

GitHub Actions based cross posting tool for backfilling existing short videos and later publishing new videos to supported social platforms.

## Credentials

Do not commit real API keys or tokens. The repository includes `secretapi.env` only as a variable-name template. Put the real values in GitHub Actions repository secrets with the same names.

## Current targets

- YouTube Shorts
- Instagram Reels
- Snapchat
- TikTok Direct Post when the app has the required access

The workflow will use checkpointed per-video/per-platform state so a later run can skip successful uploads after a partial failure.

## Instagram Reels with Instagram Login

The Instagram Login setup supports Creator and Business accounts without linking a Facebook Page. It still requires a Meta developer app configured for **Instagram API with Instagram Login**.

The manual workflow is [Publish Instagram Reel](.github/workflows/publish-instagram-reel.yml). It needs these GitHub Actions repository secrets:

- `INSTAGRAM_ACCESS_TOKEN` — Instagram User access token granted `instagram_business_basic` and `instagram_business_content_publish`
- `INSTAGRAM_USER_ID` — the professional Instagram account ID returned for the authorized account

Keep the actual values in **Settings → Secrets and variables → Actions**. The tracked `secretapi.env` file is a names-only template.

To publish, open the repository's **Actions** tab, choose **Publish Instagram Reel**, and select **Run workflow**. Enter a publicly accessible HTTPS video URL and caption, then choose whether the Reel should also appear in the feed. Each run publishes immediately and publishes a new Reel; rerunning a successful job can create a duplicate.

The workflow creates a Reel media container, waits for Instagram to finish processing, then publishes it. It does not generate or post a welcome Reel on its own. The video URL must stay publicly accessible while Instagram fetches the file. Do not enter a private or signed URL that embeds credentials because workflow run details retain the input. The default Graph API version is `v26.0`; set the optional repository variable `INSTAGRAM_GRAPH_API_VERSION` to override it.

## Connect TikTok

The manual workflow **Connect TikTok** ([connect-tiktok.yml](.github/workflows/connect-tiktok.yml)) swaps a TikTok login code for tokens and saves `TIKTOK_REFRESH_TOKEN`, `TIKTOK_ACCESS_TOKEN` and `TIKTOK_OPEN_ID` as secrets of this repository only. It then makes a read-only check (user info and `video.list` for 5 videos). It never posts, uploads or deletes.

Required repository secrets: `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET`, and `GH_PAT` (fine-grained PAT with this repository selected and **Secrets: Read and write**; classic PAT: `repo`). The redirect URI `https://venloud.github.io/nightfiles/tiktok-callback.html` is hard-coded in `pipeline/tiktok_auth.py`.

1. Open the authorize link (it must request `video.list` and use the redirect URI above):
   `https://www.tiktok.com/v2/auth/authorize/?client_key=sbawsigziq7u79m4vi&scope=user.info.basic%2Cuser.info.profile%2Cvideo.list&response_type=code&redirect_uri=https%3A%2F%2Fvenloud.github.io%2Fnightfiles%2Ftiktok-callback.html&state=social-backfill`
2. Approve, copy the URL of the page you land on.
3. Within a few minutes: Actions → **Connect TikTok** → Run workflow → paste the URL.

The log shows only PASS/FAIL, the account, the granted scopes, the token lifetimes and the video ids. It fails if `video.list` is missing or if `GH_PAT` cannot write secrets (checked before the code is used).

TikTok access tokens last 24 h and refreshing can rotate the refresh token. Every later TikTok call must refresh first: use `tiktok_auth.get_user_info()` / `tiktok_auth.get_video_ids()` or call `tiktok_auth.access_token()` first, or add a workflow step `python pipeline/tiktok_auth.py` (env `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET`, `TIKTOK_REFRESH_TOKEN`, `GH_TOKEN: ${{ secrets.GH_PAT }}`). Each refresh saves the new access and refresh tokens back to the secrets and exports `TIKTOK_ACCESS_TOKEN` / `TIKTOK_OPEN_ID` to later steps. Use the `tiktok-token` concurrency group for such jobs.

## TikTok backfill: discover → checkpoint → download → validate

```
TikTok API video.list  →  state/manifest.json  →  yt-dlp (one post URL)  →  media/<id>.mp4  →  ffprobe
   (DISCOVERY)              (CHECKPOINT)            (MEDIA ACQUISITION)                     (VALIDATION)
```

- `python -m pipeline.backfill discover [--dry-run]`: refreshes the TikTok token (one token path:
  `pipeline/tiktok_auth.py`), pages through `video.list` (20 per page, cursor), and merges every video into the
  manifest keyed by the TikTok video id. Rerunning never duplicates an entry, never resets destination status, and
  never resets media status.
- `python -m pipeline.backfill download --limit 1 [--video-id ID] [--force] [--dry-run]`: yt-dlp (Python API,
  with `curl_cffi` impersonation, no cookies, no playlists) downloads exactly the discovered post's `share_url` to
  `media/<id>.mp4`. It picks the best non-watermarked MP4 as delivered (no re-encode; ffmpeg only remuxes if
  needed). An existing valid file is skipped unless `--force`. Each real attempt increments `media.attempts`, and
  failures are stored sanitized in `media.last_error`. The status only becomes `ready` / `validated: true` after
  ffprobe confirms a readable container, a video stream, duration > 0 and width/height > 0. Audio is recorded
  but not required.
- `python -m pipeline.backfill status`: ids, dates, media and destination status (no secrets).
- Workflow **TikTok Backfill Test** (manual): discover, download + validate ONE video, upload the MP4 + manifest
  as the artifact `tiktok-backfill-test-<run number>`. It publishes nothing.

Downloaded media (`media/`), cookies and temp files are git-ignored.

### State persistence (production watcher, not implemented yet)

Actions runners are ephemeral: `state/manifest.json` only exists for the run unless it is persisted.

- **Recommended: a dedicated `state` branch** holding `state/manifest.json` (no media). Each run checks it out,
  works, and commits + pushes the manifest with the built-in `GITHUB_TOKEN` (`contents: write`). All
  TikTok/publishing jobs share one concurrency group, so only one run writes at a time. A push conflict fails the
  run and nothing is retried blindly. Git gives an audit trail and the state never expires.
- **Write-ahead rule for publishing:** before uploading to a platform, set that destination to `publishing` and
  push the state. After the upload, record `published` + `remote_id` and push again. A run that finds
  `publishing` (a crash mid-upload) never uploads again automatically; it checks the platform (or asks the owner)
  first. That way a lost state write can cause a missing post, never a duplicate.
- **Second guard:** put the TikTok video id in each destination's description/metadata so the platform itself
  can be searched before any retry.
- Not suitable as the source of truth: artifacts (expire after at most 90 days and need extra API calls to find
  the latest) and Actions cache (evicted after 7 days unused or under size pressure, and immutable per key).

## Platform connections (`pipeline/social/`)

Adapted from Venloud/MoneyPrinterTurbo `kindled_iron/publish.py` (copied, not imported), wired to this repo's
secrets only. Tokens are saved only to this repo (no mirroring), and via stdin, never on the command line.

- `common.py`: HTTP, masking, saving secrets (GH_PAT). `tiktok.py`: check + upload, using the existing
  `pipeline/tiktok_auth.py` refresh (one TikTok token path). `youtube.py`: check + Shorts upload. `meta.py`:
  Instagram (Instagram Login token **or** Facebook Login token + `INSTAGRAM_USER_ID`), Threads, Facebook Page
  Reels, including token refresh.
- The upload functions (`*_post`) are **not called anywhere yet**; `config.json` has every `publish_mode` off.
- Workflow **Platform Connection Check** (manual): refreshes tokens, prints PASS / FAIL / not configured per
  platform with the account name, posts nothing. Threads/Facebook are checked only if their secrets exist.
- Workflow **Meta Token Refresh** (manual, only for Facebook Page Reels): makes a never-expiring Page token.
- Optional secrets: `META_APP_ID`, `META_APP_SECRET` (automatic refresh of a Facebook-Login Instagram token, and
  Meta Token Refresh), `THREADS_ACCESS_TOKEN`, `FACEBOOK_PAGE_ID`, `FACEBOOK_PAGE_ACCESS_TOKEN`,
  `META_SHORT_USER_TOKEN` (temporary).

## Backfill Publish (one)

Manual workflow **Backfill Publish (one)** (`pipeline/publish_one.py`) posts exactly one TikTok video per run,
the oldest that is not fully posted, as a **public** YouTube Short (not made for kids) and an Instagram Reel
(also shown in the feed).

- **State:** `state/manifest.json` on the `state` branch, committed with `GITHUB_TOKEN`. Each destination
  records status, remote id, URL, timestamp and the format used.
- **Write-ahead:** `publishing` is pushed before each upload; `published` (+ id, URL) after. If the state push
  fails, nothing is uploaded. A destination stuck on `publishing` (interrupted run) is logged and the video is
  skipped until someone checks the platform and edits the state branch by hand.
- **Already posted / partial:** fully posted videos are logged as `already posted: <id> -> <yt> / <ig>`;
  only missing destinations are posted, and a platform that already has the video is never uploaded again.
- **Text:**
  - YouTube title: first sentence of the TikTok caption (max 90 chars) + ` #shorts`.
  - YouTube description: the caption with #fyp-style tags removed + a last line `tt:<video_id>`.
  - Instagram caption: the same cleaned caption.
- **Instagram upload:** resumable upload (the bytes go straight to Meta, no public URL). The original MP4
  (HEVC) is tried first; if the container ends in ERROR, it is re-encoded to H.264 + AAC for Instagram only.
  The container must reach FINISHED before media_publish, and the returned media id is confirmed.
- **TikTok:** the run never refreshes or writes TikTok secrets. Discovery here is read-only with the stored
  access token; if that has expired, the existing manifest is used.
