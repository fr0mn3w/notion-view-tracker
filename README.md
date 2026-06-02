# Social Metrics to Notion

A daily pipeline that captures account-level metrics from team-owned X, YouTube, and Instagram accounts and surfaces them as charts and trends in Notion.

The full specification lives in `PRD.md`. Read it first. The architecture is deterministic by design: a daily cron runs plain code, writes account-level rollup snapshots into one Notion database, and Notion's native charts read from that database. No language model runs in the daily loop.

## Status

Spec complete. No product code yet. Build v1 (X / Twitter only) first, since X is the team's primary channel.

## Kickoff prompt for Claude Code

Open this repo in Claude Code and paste the following:

```
Build a daily social-metrics pipeline per PRD.md in this repo. Constraints:
- Python. Runner: GitHub Actions scheduled workflow, daily.
- Scope this first pass to X (Twitter) ONLY (v1). Do not implement YouTube or Instagram yet.
- Use the X API owned-reads endpoints with user-context OAuth (required to read
  impressions / non-public metrics on the team's own posts).
- Write one account-level snapshot row per account per date: follower count, plus
  metrics summed across the account's posts in a trailing window (impressions, likes,
  reposts, replies, quotes, bookmarks).
- Target a single Notion database, upserted by (platform, account, date).
- Read all settings from config.yaml (copy config.example.yaml). Never hardcode secrets.
- X has no clean per-day time-series. Record cumulative metrics as current state and
  trend across daily snapshots. Re-read recent posts each run so maturing counts update.
  Do NOT build a finalized-history reconciliation; that was YouTube-specific.
- X is pay-per-use. Minimize calls. Assume owned reads ~$0.001 per resource and that a
  payment method / credits already exist on the X developer account.
- Fail soft: on a fetch error, log it and never blank existing rows.
- Do NOT run any auth flow or live API call yet. Stub credentials so the code is
  ready to run once they are provided.
- Deliver: X fetcher, Notion writer, upsert logic, workflow file, and update this
  README with the exact credential and Notion setup steps a human must complete.
```

## What a human must do (v1, X / Twitter only)

These cannot be automated. They require account access, consent, and payment.

1. Apply for an X developer account and create an app.
2. Configure user-context OAuth and authorize each team-owned account so the app can read impressions on its own posts.
3. Add a payment method and buy credits on the X developer account (pay-per-use).
4. Create a Notion integration, copy its token, create the Daily Snapshots database, and share that database with the integration.
5. Put every credential into the GitHub repo's encrypted secret store, never in code.

YouTube is the quick v2 add. Instagram comes last, gated by Meta App Review.

---

## v1 implementation (X / Twitter)

This repo now contains a working X-only pipeline. It runs daily on GitHub Actions,
fetches each team-owned account's current followers plus its post metrics summed over
a trailing window, and upserts one snapshot row per account per date into a single
Notion database. No live API call runs until you provide credentials.

### Layout

```
src/
  config.py         # load non-secret settings from config.yaml
  x_fetcher.py      # X owned-reads fetch + per-account rollup
  notion_writer.py  # upsert by (platform, account, date), age out Provisional
  main.py           # orchestrator, fail-soft, exit 1 if 0 rows written
config.example.yaml # copy to config.yaml and fill in (no secrets in it)
requirements.txt
.github/workflows/daily-snapshot.yml
```

### How it works

- Per account per run: one `/users/me` call (follower count) plus one timeline pull of
  original posts published within `window_days` (default 7). Metrics are summed across
  those posts: impressions, likes, reposts, replies, quotes, bookmarks. `max_posts`
  caps the pull as a cost guard (X is pay-per-use, ~$0.001 per resource read).
- X has no clean per-day time-series, so cumulative state (followers, summed post
  metrics) is snapshotted each day and trended across snapshots. There is no
  finalized-history reconciliation (that was YouTube-specific).
- The freshest rows are flagged **Provisional** while counts are still maturing. Each
  run clears the flag on rows older than `provisional_days`. This costs no X calls.
- **Fail soft:** if one account errors, the others still write, the error is logged,
  and existing rows are never blanked. The run exits 1 only if zero rows were written,
  so a dead pipeline shows up as a failed Action.

### What a human must do (cannot be automated)

**1. X developer app**
- Create an X developer account and an app (pay-per-use; add a payment method / credits).
- Enable OAuth 2.0 with a confidential client. Note the **Client ID** and **Client Secret**.
- For each team-owned account, run the OAuth2 Authorization Code flow with PKCE and the
  scopes `tweet.read users.read offline.access`, signed in as that account, to mint a
  user-context **access token** and **refresh token**. `offline.access` is what makes the
  refresh token possible; user context is what unlocks `non_public_metrics` (impressions)
  on the account's own posts.

**2. Notion database**
- Create an internal Notion integration and copy its token.
- Create a database named **Daily Snapshots** with exactly these properties (names matter):

  | Property | Type |
  |---|---|
  | Name | Title |
  | Date | Date |
  | Platform | Select (options: X, YouTube, Instagram) |
  | Account | Select |
  | Followers or Subscribers | Number |
  | Views | Number |
  | Impressions or Reach | Number |
  | Engagements | Number |
  | Posts Published | Number |
  | Provisional | Checkbox |

- Share the database with the integration (database `•••` menu -> Connections -> add it).
- Copy the database ID (the 32-char id in the database URL).

**3. Config**
- `cp config.example.yaml config.yaml`, set the real `database_id`, and list each
  account's `handle` plus the env-var names that will hold its tokens. Commit `config.yaml`
  (it holds no secrets).

**4. GitHub Actions Secrets** (Settings -> Secrets and variables -> Actions)
- `NOTION_TOKEN`, `NOTION_DATABASE_ID`
- `X_CLIENT_ID`, `X_CLIENT_SECRET`
- Per account, matching the `*_env` names in `config.yaml`, e.g.
  `X_EXAMPLEACCOUNT_ACCESS_TOKEN`, `X_EXAMPLEACCOUNT_REFRESH_TOKEN`.
- Add the matching `env:` entries to `.github/workflows/daily-snapshot.yml` for any
  account beyond the example.

### Run it

```bash
pip install -r requirements.txt
# fill config.yaml, then export the same secrets as env vars locally:
export NOTION_TOKEN=... NOTION_DATABASE_ID=... X_CLIENT_ID=... X_CLIENT_SECRET=...
export X_EXAMPLEACCOUNT_ACCESS_TOKEN=... X_EXAMPLEACCOUNT_REFRESH_TOKEN=...
python -m src.main
```

In CI it runs daily on the schedule in the workflow, and can be triggered manually from
the Actions tab (`workflow_dispatch`).

### Dashboard

Hero metric is **impressions / views** (on X, `impression_count`, which X surfaces as
"Views"). It lands in the **Impressions or Reach** column. Build the headline chart as a
line view of that column over Date, grouped by Account. Followers and Engagements get
secondary line charts.

### Known v1 limitation: refresh-token rotation

X rotates the OAuth2 refresh token on every use. A stateless Actions run cannot persist
the new token back into Secrets, so the stored refresh token goes stale once used. v1
keeps the access token alive within its lifetime and logs a loud warning when rotation
happens. Durable options for later: store the rotating token in a secret manager and
write it back each run, or keep a long-lived session refreshed by a separate job. This is
the PRD's flagged number-one operational risk; revisit it before relying on unattended
runs over long periods.

