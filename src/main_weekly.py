"""Weekly Report orchestrator (phase 1).

Runs weekly. Reads the Daily Snapshots database, computes each channel's weekly views and
follower/subscriber change for the most recently completed Sunday-Saturday week, and
upserts one row into the Weekly Report database. No platform API calls; no LLM (the Summary
column is a Notion AI property).
"""
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

from .config import ConfigError, load_config
from .weekly_report import (
    NotionDB,
    compute_channel,
    read_channel_rows,
    upsert_week,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("weekly-report")


def _env(name):
    if not name:
        return None
    v = os.environ.get(name)
    return v.strip() if v else None


def target_week(now):
    """Most recently completed Sunday-Saturday week, given the run datetime.

    Returns (week_start Sunday, week_end Saturday, prior_end Saturday) as dates.
    """
    today = now.date()
    # Most recent Saturday on or before today (Mon=0 .. Sun=6, Sat=5).
    end = today - timedelta(days=(today.weekday() - 5) % 7)
    if end == today:  # run on a Saturday: that week isn't finished, step back one
        end = end - timedelta(days=7)
    start = end - timedelta(days=6)  # the Sunday
    prior_end = start - timedelta(days=1)  # the previous Saturday
    return start, end, prior_end


def run():
    try:
        cfg = load_config(os.environ.get("CONFIG_PATH", "config.yaml"))
    except ConfigError as e:
        log.error("%s", e)
        return 1

    now = datetime.now(timezone.utc)

    notion_token = _env("NOTION_TOKEN")
    snapshots_db_id = _env("NOTION_DATABASE_ID") or cfg.get("notion", {}).get("database_id")
    wcfg = cfg.get("weekly_report", {})
    report_db_id = wcfg.get("database_id")
    if not notion_token or not report_db_id or "REPLACE" in str(report_db_id):
        log.error(
            "Weekly report not configured. Need NOTION_TOKEN and weekly_report.database_id."
        )
        return 1

    db = NotionDB(notion_token)
    start, end, prior_end = target_week(now)
    week_start, week_end, prior = start.isoformat(), end.isoformat(), prior_end.isoformat()
    log.info("Weekly report for %s to %s (prior boundary %s)", week_start, week_end, prior)

    try:
        snap_props = db.schema(snapshots_db_id)
        report_props = db.schema(report_db_id)
    except Exception as e:
        log.error("Couldn't read database schemas: %s", e)
        return 1

    # Fetch window covers the prior boundary plus a couple of days of slack.
    fetch_start = (prior_end - timedelta(days=2)).isoformat()

    values = {}
    total_views = 0
    have_any = False
    for ch in wcfg.get("channels", []):
        try:
            rows = read_channel_rows(
                db, snapshots_db_id, snap_props,
                ch["platform"], ch["accounts"], fetch_start, week_end,
            )
            weekly_views, net_followers = compute_channel(
                rows, week_start, week_end, prior, ch["views_method"]
            )
        except Exception as e:  # fail soft per channel
            log.error("Channel %s failed: %s", ch.get("views_column"), e)
            continue

        if weekly_views is not None:
            values[ch["views_column"]] = {"number": weekly_views}
            total_views += weekly_views
            have_any = True
        if net_followers is not None:
            values[ch["followers_column"]] = {"number": net_followers}
        log.info(
            "%s: weekly_views=%s net_followers=%s",
            ch["views_column"], weekly_views, net_followers,
        )

    if have_any and wcfg.get("total_views_column"):
        values[wcfg["total_views_column"]] = {"number": total_views}
    if wcfg.get("week_start_column"):
        values[wcfg["week_start_column"]] = {"date": {"start": week_start}}
    if wcfg.get("week_end_column"):
        values[wcfg["week_end_column"]] = {"date": {"start": week_end}}
    if wcfg.get("generated_column"):
        values[wcfg["generated_column"]] = {"date": {"start": now.date().isoformat()}}

    title = f"Week of {week_start}"
    try:
        action = upsert_week(db, report_db_id, report_props, title, values)
        log.info("Weekly Report row %s: %s", action, title)
    except Exception as e:
        log.error("Weekly report write failed: %s. Existing row left intact.", e)
        return 1

    log.info("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
