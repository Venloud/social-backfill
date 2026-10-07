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
