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

The manual workflow [Connect TikTok](.github/workflows/connect-tiktok.yml) swaps a TikTok login code for tokens and saves `TIKTOK_REFRESH_TOKEN`, `TIKTOK_ACCESS_TOKEN` and `TIKTOK_OPEN_ID` as secrets of this repository only. It does not post, upload or delete anything.

Required repository secrets:

- `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET` — from the TikTok developer app
- `TIKTOK_REDIRECT_URI` — `https://venloud.github.io/nightfiles/tiktok-callback.html` (must match the app's registered redirect URI exactly)
- `GH_PAT` — fine-grained personal access token with access to this repository and **Secrets: Read and write** (Metadata: Read is added automatically)

Approve the app on TikTok (the authorize URL must request `video.list`), then run the workflow with the full callback URL or just the code within a few minutes. The log shows only PASS/FAIL, the account, the granted scopes and the token lifetimes; the run fails if `video.list` is missing.

TikTok access tokens last 24 h and refreshing can rotate the refresh token. Any job that calls the TikTok API must refresh first: add a step `python pipeline/tiktok_auth.py` (env `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET`, `TIKTOK_REFRESH_TOKEN`, `GH_TOKEN: ${{ secrets.GH_PAT }}`) or call `tiktok_auth.access_token()` before each call. It saves the new tokens back to the secrets, exports `TIKTOK_ACCESS_TOKEN` / `TIKTOK_OPEN_ID` to later steps, and the job should use the `tiktok-token` concurrency group.
