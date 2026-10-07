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
