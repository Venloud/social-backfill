# AI HANDOFF — Social Backfill

Last updated: 2026-10-07 by ChatGPT after Claude's Phase 2 work.

## Read this first

Preserve the existing OAuth, state branch, write-ahead publishing, Instagram resumable upload, HEVC/H.264 fallback, and duplicate protection. Do not replace working code just to reorganize it. Never print or commit secret values.

## Verified before this handoff

Claude's real Phase 2 run was GitHub Actions run 37698243517 on commit `e009a60`.

- TikTok discovery: read-only, 53 videos reported.
- Instagram: real public Reel publish succeeded for TikTok `7693026624211569934`.
- Instagram media id: `18108327976908657`.
- Instagram permalink: `https://www.instagram.com/reel/DeNdyVjDvhr/`.
- Instagram accepted the original HEVC + AAC MP4, so H.264 fallback was not needed in that run.
- YouTube: failed before a video was created with `youtubeSignupRequired`. The Google OAuth token itself had already passed the connection check with `youtube.upload`; the user said the YouTube channel had simply not been created yet.
- State branch `state` correctly recorded Instagram as published and YouTube as failed. Write-ahead history existed for discovery, media, publishing, failure/success.
- No Snapchat publisher is proven yet.
- Production schedule is NOT enabled.

## User's required queue behavior

Backfill must be independent per destination.

Example:
- Video A: Instagram published, YouTube failed.
- Video B: both pending.

Next run must:
1. Retry Video A on YouTube only.
2. Advance Instagram to Video B in the same run.
3. Never upload Video A to Instagram again.

A failed or uncertain platform must not freeze healthy platforms. Each destination maintains its own oldest-first synchronization queue.

Fresh TikTok posts will later have higher priority than historical backfill. Desired production behavior after this phase is stable:
new TikTok detected -> cross-post to every healthy destination -> return to historical backlog. Historical goal: 20 source TikToks/day, with per-platform success tracked independently.

## Changes made by ChatGPT after Claude stopped

### `pipeline/publish_one.py`

Changed selection from one global source-video queue to independent per-destination queues.

New `pick_jobs(manifest)`:
- scans oldest-first separately for YouTube and Instagram;
- skips `published` for that destination permanently;
- skips `publishing` only for the affected destination, leaving the other destination free to advance;
- treats `failed`/pending as eligible;
- returns at most one candidate per destination.

The run can now process two different source videos, one for YouTube and one for Instagram. If both destinations select the same TikTok, media acquisition is cached by TikTok id so it is downloaded/validated once in that run.

Write-ahead behavior was NOT weakened:
- push `publishing` before platform upload;
- push `published` after confirmed success;
- failures before live become retryable `failed`;
- uncertain UploadStarted/PublishAttempted remains `publishing` for manual review;
- state push failure aborts before upload.

No TikTok auth behavior, Instagram upload implementation, YouTube upload implementation, caption logic, or state-store push semantics were changed.

### Regression tests

Added `tests/test_publish_queue.py` with four no-network tests:
1. YouTube failure on old video + Instagram already published -> YouTube retries old, Instagram advances next.
2. Fully published old video is never selected again.
3. Stuck YouTube `publishing` does not block Instagram.
4. Failed destination remains retryable.

Added `.github/workflows/publisher-queue-tests.yml`. It runs only the pure queue regression tests and has no publishing credentials or write permission.

### Documentation

Updated the comments in `.github/workflows/backfill-publish-one.yml` and the README to describe independent destination queues.

## Commits from ChatGPT's takeover

- `8b031af` — Decouple per-platform backfill queues
- `b088a0e` — Add independent backfill queue regression tests
- `06da8a5` — Add safe publisher queue test workflow
- `a89ff72` — Trigger publisher queue regression tests
- `2ca7bd3` — Document independent destination queue behavior
- `920f1e2` — Explain independent per-platform backfill queues

- `6f2084d` — Add this AI handoff
- `13ba943` — Final harmless test-file wording update after CI was installed

Use the latest main SHA as the authoritative head.

## Test evidence from ChatGPT takeover

GitHub Actions run `37699491282` (`Publisher Queue Tests`) completed SUCCESS on commit `a89ff72`.
The tested publisher code is the same queue implementation in current main; later commits only changed docs/workflow comments/handoff/test wording.

Actual CI log:
```
test_failed_destination_is_retryable ... ok
test_partial_failure_does_not_hold_healthy_platform_back ... ok
test_published_destination_is_never_selected_again ... ok
test_stuck_destination_does_not_block_other_platform ... ok

Ran 4 tests
OK
```

No real platform upload was triggered by ChatGPT during this takeover. The next live publisher run remains intentionally manual.

## Next safe tests

Do NOT enable the daily schedule yet.

1. Create the YouTube channel under the same Google account that owns the current refresh token.
2. Retry the existing publisher manually.
3. Expected queue selection with current state:
   - YouTube retries TikTok `7693026624211569934`.
   - Instagram selects the next oldest video that is not already published there.
4. Confirm Instagram does not duplicate the already-published Reel.
5. Confirm YouTube returns a real video id/url, or record its exact API failure.
6. Run again and verify each destination independently advances past its own published items.
7. Only after real duplicate/partial-failure behavior is proven should scheduling and the 20/day limiter be implemented.

## Production work still missing

- Fresh-post priority over historical backfill.
- 20 historical source-video/day limiter.
- Scheduled watcher/backfill workflow.
- Snapchat publishing/connection proof.
- Final handling for YouTube privacy if the Google Cloud project forces API uploads private.
- Long-term Instagram token refresh configuration if needed for the current token type.
- Security cleanup/rotation for any credentials historically committed in `secretapi.env`; the tracked file should contain names/placeholders only.

## Safety invariants

- Never retry a destination in `publishing` blindly.
- Never reset a destination from `published` during discovery.
- Never make one destination's failure block another destination.
- Never upload before the write-ahead state push succeeds.
- Never expose tokens, secrets, auth headers, or cookies in logs or documentation.
- Do not turn on high-volume publishing until the manual real-world queue test passes.


## 2026-10-07: permanent watcher enabled by ChatGPT

**Live proof before enabling:** Actions run [37700545706](https://github.com/Venloud/social-backfill/actions/runs/37700545706) succeeded. It discovered 53 TikToks, retried YouTube video `7693026624211569934` and published `https://youtube.com/shorts/6BbCO9eeA7s`; independently advanced Instagram to `7693031212864130317` and published `https://www.instagram.com/reel/DeNgZBFj_UZ/`. No duplicate Instagram Reel for the first video.

**Changes:**
- `pipeline/publish_one.py`: newly discovered TikToks receive `source.fresh_priority=true` during discovery. For their first 24 hours since `first_seen_at`, each destination picks fresh pending/failed videos before historical backlog. Published destinations are skipped, uncertain `publishing` entries remain manual-check-only. Old manifest entries are not reclassified as fresh.
- `pick_jobs(m, daily_limit=20)`: caps **distinct historical source TikTok IDs** with successful uploads per UTC day, not individual destination uploads. An already-counted source can finish missing destinations even after cap. Fresh videos bypass the historical cap. The existing per-destination queues and write-ahead state pushes remain unchanged.
- `.github/workflows/backfill-publish-one.yml`: enabled `*/5 * * * *` GitHub Actions cron and kept manual dispatch. Before discovery, it runs existing `pipeline/tiktok_auth.py` to rotate and save TikTok credentials with `GH_PAT`, and exports the fresh access token via `GITHUB_ENV`. Uses shared `tiktok-token` concurrency group, `cancel-in-progress: false`, `contents: write`, and `--daily-limit 20`. Cron timing is best effort, not guaranteed exact five-minute delivery.
- `tests/test_publish_queue.py`: adds tests for fresh priority, historical cap, and distinct-ID counting.
- No Snapchat publisher has been added. Only YouTube and Instagram currently publish.
- No existing state branch was cleared or reset. No real uploads were intentionally triggered during these code changes.

**Important operational notes:**
- The 20/day count is a daily cap on historical source IDs that achieved at least one successful destination publish, plus sources reserved within the current run. It is **not** a guaranteed 20 completions, because API failures, delays, and GitHub scheduling can reduce throughput.
- Failed destinations are retried on subsequent runs, so repeated permanent errors may consume Actions minutes; consider exponential backoff later.
- New-post detection depends on successful TikTok `video.list` and refresh, not a webhook. The next scheduled run discovers it.
- Do not edit `state` without checking existing remote IDs and URLs. Never blindly retry a `publishing` state.
- GitHub cron can be delayed or skipped under load; verify runs under Actions.
- The token refresh step depends on `GH_PAT` having repository secrets write permission. If it expires, the workflow fails before publishing rather than relying on a stale token.
- Instagram long-term token refresh and Snapchat API access still require follow-up.

**Relevant commits:** `74cfe3e` (fresh priority + quota), `0f9f07d` (schedule + token refresh), `2656f18` (regression tests). Later commits update documentation and test results.

### Verification and token-env correction

- GitHub Actions [Publisher Queue Tests run 37701203268](https://github.com/Venloud/social-backfill/actions/runs/37701203268) passed all **7** regression tests (including fresh priority and historical quota). This is an offline logic test, not a live scheduled publishing run.
- Commit `3f77939`: removed `TIKTOK_ACCESS_TOKEN` from the publishing step's explicit `env` so the newly refreshed token exported to `GITHUB_ENV` is actually used. Also simplified the identical manual/scheduled run commands. This prevents an old repository-secret value from shadowing the freshly refreshed access token.
- No successful scheduled publishing run has been verified yet. Review the first scheduled run logs before assuming unattended reliability.

## 2026-10-22 cleanup audit (Vandam repository cleanup)

- Scheduled `Backfill Publish (one)` failed 4 of the last 8 runs (2026-10-21). Causes seen in logs:
  - `youtube: HTTP 400: invalid_grant` (runs 39694709083, 39641518144). The YouTube refresh token is expired or revoked. **Owner action:** reconnect YouTube and replace the `YOUTUBE_REFRESH_TOKEN` secret. Code cannot fix this.
  - Instagram resumable upload `HTTP 400` (run 39694709083), transient; the write-ahead state recorded it as retryable.
  - `! [remote rejected] HEAD -> state (failed)` (run 39671931241): a transient GitHub push failure. Fixed in `pipeline/state_store.py` with 3 bounded attempts; a persistent failure still blocks the upload (`tests/test_state_store.py`).
- `feature/two-tiktok-sources` has a tree identical to `main` (squash-merged as efea7e9). Safe to delete after owner approval; tip `16244afd`.
