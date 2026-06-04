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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("social-metrics")


def _env(name):
    if not name:
        return None
    v = os.environ.get(name)
    return v.strip() if v else None


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

        def persist_rotated(new_access, new_refresh, _aenv=access_env, _renv=refresh_env, _h=handle):
            if not (gh_pat and gh_repo):
                log.warning(
                    "Token rotated for @%s but GH_PAT/GITHUB_REPOSITORY not set; new refresh "
                    "token NOT persisted. Next run will fail auth. Set GH_PAT to auto-persist.",
                    _h,
                )
                return
            try:
                update_repo_secret(gh_repo, _renv, new_refresh, gh_pat)
                update_repo_secret(gh_repo, _aenv, new_access, gh_pat)
                log.info("Persisted rotated tokens for @%s", _h)
            except Exception as pe:
                log.error(
                    "CRITICAL: failed to persist rotated token for @%s: %s. Next run may "
                    "fail auth; re-mint that account's tokens if so.",
                    _h, pe,
                )

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
            writer.upsert(snapshot)
            rows_written += 1
        except Exception as e:  # fail soft: log and move on, never blank rows
            log.error(
                "Fetch/write failed for X account @%s: %s. Existing rows left intact.",
                handle,
                e,
            )
            continue

    # Age out Provisional flags on rows that have settled.
    try:
        cutoff = (now.date() - timedelta(days=provisional_days)).isoformat()
        writer.age_out_provisional("X", cutoff)
    except Exception as e:
        log.error("Provisional age-out failed: %s", e)

    if rows_written == 0:
        log.error("Wrote 0 rows this run. Check credentials / API status.")
        return 1
    log.info("Done. Rows written/updated: %d", rows_written)
    return 0


if __name__ == "__main__":
    sys.exit(run())
