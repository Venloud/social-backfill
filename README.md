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
