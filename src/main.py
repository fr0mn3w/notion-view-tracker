"""Daily social-metrics pipeline orchestrator (v1: X / Twitter only).

Flow per run: load config, init Notion, loop the configured X accounts, fetch each
account's snapshot, upsert it by (platform, account, date), then age out settled
Provisional rows. Fails soft: one account's error never blanks another's rows.

Exit code: 0 if at least one row was written, 1 otherwise (so CI surfaces a dead run).
"""
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

from .config import ConfigError, load_config
from .github_secrets import update_repo_secret
from .notion_writer import NotionWriter
from .x_fetcher import XClient, fetch_account_snapshot
from .youtube_fetcher import YouTubeClient, fetch_channel_snapshots

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("social-metrics")


def _env(name):
    if not name:
        return None
    v = os.environ.get(name)
    return v.strip() if v else None


def _make_persist(gh_pat, gh_repo, access_env, refresh_env, handle):
    """Build an on_refresh callback that writes rotated tokens back to GitHub Secrets."""
    def persist(new_access, new_refresh):
        if not (gh_pat and gh_repo):
            log.warning(
                "Token rotated for %s but GH_PAT/GITHUB_REPOSITORY not set; new refresh "
                "token NOT persisted. Next run may fail auth. Set GH_PAT to auto-persist.",
                handle,
            )
            return
        try:
            update_repo_secret(gh_repo, refresh_env, new_refresh, gh_pat)
            update_repo_secret(gh_repo, access_env, new_access, gh_pat)
            log.info("Persisted rotated tokens for %s", handle)
        except Exception as pe:
            log.error(
                "CRITICAL: failed to persist rotated token for %s: %s. Next run may "
                "fail auth; re-mint that account's tokens if so.",
                handle, pe,
            )
    return persist


def run():
    try:
        cfg = load_config(os.environ.get("CONFIG_PATH", "config.yaml"))
    except ConfigError as e:
        log.error("%s", e)
        return 1

    now = datetime.now(timezone.utc)

    # --- Notion ---
    notion_token = _env("NOTION_TOKEN")
    notion_cfg = cfg.get("notion", {})
    database_id = _env("NOTION_DATABASE_ID") or notion_cfg.get("database_id")
    if not notion_token or not database_id or "REPLACE" in str(database_id):
        log.error(
            "Notion not configured. Set NOTION_TOKEN and notion.database_id "
            "(or NOTION_DATABASE_ID). Aborting before any write."
        )
        return 1
    writer = NotionWriter(notion_token, database_id)

    # --- X ---
    xcfg = cfg.get("x", {})
    accounts = xcfg.get("accounts", [])
    window_days = int(xcfg.get("window_days", 7))
    provisional_days = int(xcfg.get("provisional_days", 2))
    max_posts = int(xcfg.get("max_posts", 100))

    client_id = _env("X_CLIENT_ID")
    client_secret = _env("X_CLIENT_SECRET")
    client = XClient(client_id, client_secret) if client_id and client_secret else None

    # For persisting rotated tokens back into GitHub Secrets (see github_secrets.py).
    gh_pat = _env("GH_PAT")
    gh_repo = _env("GITHUB_REPOSITORY")  # auto-set by Actions, e.g. "fr0mn3w/notion-view-tracker"

    rows_written = 0
    for account in accounts:
        handle = account.get("handle", "?")
        access_token = _env(account.get("access_token_env"))
        refresh_token = _env(account.get("refresh_token_env"))
        access_env = account.get("access_token_env")
        refresh_env = account.get("refresh_token_env")

        if not client or not access_token:
            log.warning(
                "No credentials for X account @%s yet, skipping (existing rows untouched).",
                handle,
            )
            continue

        persist_rotated = _make_persist(gh_pat, gh_repo, access_env, refresh_env, "@" + handle)

        try:
            snapshot, _rotated = fetch_account_snapshot(
                client,
                account,
                {"access_token": access_token, "refresh_token": refresh_token},
                window_days,
                max_posts,
                now,
                on_refresh=persist_rotated,
            )
            # Daily follower delta: today's total minus the most recent prior snapshot.
            try:
                prev = writer.previous_followers("X", snapshot["account"], snapshot["date"])
                if prev is not None:
                    snapshot["followers_gained"] = snapshot["followers"] - prev
            except Exception as pe:
                log.warning("Couldn't compute follower delta for @%s: %s", handle, pe)
            writer.upsert(snapshot)
            rows_written += 1
        except Exception as e:  # fail soft: log and move on, never blank rows
            log.error(
                "Fetch/write failed for X account @%s: %s. Existing rows left intact.",
                handle,
                e,
            )
            continue

    # Age out Provisional flags on settled X rows.
    try:
        cutoff = (now.date() - timedelta(days=provisional_days)).isoformat()
        writer.age_out_provisional("X", cutoff)
    except Exception as e:
        log.error("Provisional age-out failed: %s", e)

    # --- YouTube ---
    ycfg = cfg.get("youtube", {})
    channels = ycfg.get("channels", [])
    trailing_days = int(ycfg.get("trailing_days", 3))
    yt_provisional_days = int(ycfg.get("provisional_days", trailing_days))

    yt_api_key = _env("YT_API_KEY")
    yt_client_id = _env("YT_CLIENT_ID")
    yt_client_secret = _env("YT_CLIENT_SECRET")
    yt_client = (
        YouTubeClient(yt_api_key, yt_client_id, yt_client_secret)
        if yt_api_key and yt_client_id and yt_client_secret
        else None
    )

    for channel in channels:
        handle = channel.get("handle", "?")
        access_token = _env(channel.get("access_token_env"))
        refresh_token = _env(channel.get("refresh_token_env"))

        channel_id = channel.get("channel_id") or ""
        if not yt_client or not access_token or "REPLACE" in channel_id:
            log.warning(
                "No credentials/channel_id for YouTube channel %s yet, skipping.", handle
            )
            continue

        persist_rotated = _make_persist(
            gh_pat, gh_repo,
            channel.get("access_token_env"), channel.get("refresh_token_env"), handle,
        )
        try:
            snapshots, _rotated = fetch_channel_snapshots(
                yt_client,
                channel,
                {"access_token": access_token, "refresh_token": refresh_token},
                trailing_days,
                now,
                on_refresh=persist_rotated,
            )
            for snap in snapshots:
                writer.upsert(snap)
                rows_written += 1
        except Exception as e:  # fail soft
            log.error(
                "Fetch/write failed for YouTube channel %s: %s. Existing rows left intact.",
                handle, e,
            )
            continue

    try:
        cutoff = (now.date() - timedelta(days=yt_provisional_days)).isoformat()
        writer.age_out_provisional("YouTube", cutoff)
    except Exception as e:
        log.error("YouTube provisional age-out failed: %s", e)

    if rows_written == 0:
        log.error("Wrote 0 rows this run. Check credentials / API status.")
        return 1
    log.info("Done. Rows written/updated: %d", rows_written)
    return 0


if __name__ == "__main__":
    sys.exit(run())
