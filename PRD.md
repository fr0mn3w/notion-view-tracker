# PRD v2: Social Metrics to Notion (locked architecture)

## Goal
Capture daily account-level metrics from team-owned X, YouTube, and Instagram accounts and surface them as charts and trends inside Notion. Fully custom, deterministic, with no language model in the daily runtime.

## Locked decisions
- Output: Notion dashboard using Notion's native charts.
- Accounts: team-owned only.
- Approach: fully custom code.
- Storage: Notion only, daily account-level rollup snapshots. No external database.
- Granularity: rollup snapshots only. Per-post history is not retained.
- Freshness: same-day capture, with a trailing re-fetch so recent points self-correct.
- Claude's role: building the system, and ad-hoc analysis later. Not the daily runtime.

## Cut from v1 (deliberately)
- Claude in the daily loop. The job is deterministic ETL; an LLM in the hot path added cost, nondeterminism, and a failure surface for no gain.
- Claude-written narrative summaries. Not requested.
- Anomaly detection and Flags. Deferred to a possible future.
- Per-post raw rows and stateful 30-day delta computation. Replaced by simple daily snapshots.
- External data store. Snapshot volume is low enough for Notion alone.
- Top Item, CTR, and watch time as default fields. Available later only if a hero metric demands them.

## Architecture
A single daily cron runs plain code. No server.

1. Trigger: GitHub Actions scheduled workflow, daily. Secrets in the repo's encrypted store.
2. Runner: one script (Python recommended). Four modules: three platform fetchers and one Notion writer.
3. Fetch: for each account, pull current account-level metrics, and re-pull the trailing 3 days where the platform exposes per-day figures.
4. Write: upsert one row per account per platform per date into a single Notion database. The upsert key is (platform, account, date), so re-runs and the trailing re-fetch overwrite cleanly instead of duplicating.
5. Present: Notion native chart views read directly from that database.

Data flow, one run: cron fires, script authenticates each platform, fetches current account metrics plus the trailing window, upserts snapshot rows keyed by date, ends. Charts update on their own because they sit on the same database.

## Notion schema (single database: Daily Snapshots)
- Date (Date)
- Platform (Select: X / YouTube / Instagram)
- Account (Select)
- Followers or Subscribers (Number, cumulative)
- Views (Number)
- Impressions or Reach (Number, see platform note)
- Engagements (Number: likes + comments + shares + saves where available)
- Posts Published (Number, count for that day)
- Provisional (Checkbox, true while the row is inside the still-settling trailing window)

Charts: line views grouped by Account or Platform across Date, for followers, views, and engagement.

## Per-platform fetch scope and access
- X: owned-reads endpoints with user-context auth per account. Own follower count and aggregate post metrics (impressions, likes, reposts, replies, quotes, bookmarks). Cost trivial at owned-read rates.
- YouTube: Data API key for public counts (views, subscribers, likes, comments); Analytics API with owner OAuth for impressions, CTR, and the per-day time-series that powers the trailing re-fetch.
- Instagram: Graph API via a Meta app. Business or Creator accounts linked to a Facebook Page. Account insights (reach, views, profile visits, follower count). Meta App Review is a prerequisite and a go/no-go gate.

Metric reality: "impressions" is not uniform. X exposes it for own posts, YouTube only via the Analytics API, and Meta is migrating impressions toward views. The schema keeps one Impressions/Reach column and tolerates nulls per platform rather than pretending the metric is identical everywhere.

## Daily job logic
1. For each account, fetch current account-level metrics.
2. Where per-day time-series exists (chiefly YouTube Analytics), re-fetch the trailing 3 days and overwrite those snapshot rows with the latest figures. This is what self-corrects the droop.
3. Cumulative-state metrics (follower counts, lifetime views) are recorded as current state and trended as cumulative-over-time, which has no droop to correct.
4. Upsert all rows by (platform, account, date). Mark the trailing window Provisional until it ages out.
5. Fail soft: if one platform errors, write the others, log the failure, and never blank existing rows.

## The number-one operational risk: OAuth refresh
Instagram long-lived tokens expire around 60 days; YouTube refresh tokens can be revoked. A missed refresh stops the pipeline silently. Built in from day one:
- Refresh the Instagram long-lived token on a schedule well inside its window.
- Store and rotate the YouTube refresh token.
- Emit an alert (email or Slack) when any token is within N days of expiry, or when a run writes zero rows for a platform.

## Config (filled at build time, not guessed)
- Per platform: the exact accounts, handles, and channel IDs, and how many.
- The single hero metric the dashboard should foreground.
- Which credentials, apps, and the Notion integration already exist versus start from zero.
- The notification channel for the expiry alert.

## Build plan
- v1: X (Twitter) end to end. Chosen as the team's primary channel. Carries pay-per-use billing and user-context OAuth, and exposes no clean per-day time-series, so cumulative metrics are snapshotted daily and trended across snapshots. Single Notion database live with native charts.
- v2: add YouTube, then Instagram. Instagram only after Meta App Review clears.
- v3 (optional, only if asked): hero-metric-specific fields, weekly and monthly rollups, alerting beyond token expiry.

## What only the team can do
Account creation, OAuth consent, Meta App Review, and entering every credential into the runner's secret store. The code and schema arrive ready; the keys are turned by the team.
