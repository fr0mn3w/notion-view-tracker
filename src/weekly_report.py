"""Weekly Report (phase 1): roll up Daily Snapshots into one weekly row.

Reads the Daily Snapshots database (no platform API calls), computes per-channel weekly
views and follower/subscriber deltas for a Sunday-Saturday week, and upserts one row into
the Weekly Report database. The Summary column is filled by Notion AI, not here.
"""
import logging

import requests

log = logging.getLogger(__name__)

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# Daily Snapshots column candidates (same renames the daily writer tolerates).
SNAP_DATE = ["Date"]
SNAP_PLATFORM = ["Platform"]
SNAP_ACCOUNT = ["Account"]
SNAP_VIEWS = ["Views", "Impressions or Reach", "Impressions", "Reach"]
SNAP_FOLLOWERS = ["Followers or Subscribers", "Followers", "Subscribers"]


class NotionDB:
    """Minimal Notion client: schema read, query, page create/update."""

    def __init__(self, token, session=None):
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            }
        )

    def schema(self, db_id):
        r = self.session.get(f"{NOTION_API}/databases/{db_id}", timeout=30)
        r.raise_for_status()
        return r.json().get("properties", {})

    @staticmethod
    def resolve(props, candidates):
        for name in candidates:
            if name in props:
                return name
        return None

    def query(self, db_id, filter_obj, sorts=None, page_size=100):
        results = []
        payload = {"filter": filter_obj, "page_size": page_size}
        if sorts:
            payload["sorts"] = sorts
        cursor = None
        while True:
            if cursor:
                payload["start_cursor"] = cursor
            r = self.session.post(
                f"{NOTION_API}/databases/{db_id}/query", json=payload, timeout=30
            )
            r.raise_for_status()
            body = r.json()
            results.extend(body.get("results", []))
            if not body.get("has_more"):
                break
            cursor = body.get("next_cursor")
        return results


def read_channel_rows(db, snapshots_db_id, props, platform, accounts, start_date, end_date):
    """Return [{date, views, followers}] for platform+accounts within [start, end]."""
    date_col = db.resolve(props, SNAP_DATE)
    platform_col = db.resolve(props, SNAP_PLATFORM)
    account_col = db.resolve(props, SNAP_ACCOUNT)
    views_col = db.resolve(props, SNAP_VIEWS)
    followers_col = db.resolve(props, SNAP_FOLLOWERS)

    account_filters = [{"property": account_col, "select": {"equals": a}} for a in accounts]
    filt = {
        "and": [
            {"property": platform_col, "select": {"equals": platform}},
            {"or": account_filters} if len(account_filters) > 1 else account_filters[0],
            {"property": date_col, "date": {"on_or_after": start_date}},
            {"property": date_col, "date": {"on_or_before": end_date}},
        ]
    }
    pages = db.query(
        snapshots_db_id, filt, sorts=[{"property": date_col, "direction": "ascending"}]
    )
    rows = []
    for p in pages:
        pr = p["properties"]
        d = pr.get(date_col, {}).get("date") or {}
        rows.append(
            {
                "date": (d.get("start") or "")[:10],
                "views": pr.get(views_col, {}).get("number"),
                "followers": pr.get(followers_col, {}).get("number"),
            }
        )
    return rows


def _value_on_or_before(rows, target_date, field):
    """Field value from the latest row whose date is <= target_date."""
    best = None
    for r in rows:
        if r["date"] and r["date"] <= target_date and r.get(field) is not None:
            if best is None or r["date"] > best["date"]:
                best = r
    return best[field] if best else None


def compute_channel(rows, week_start, week_end, prior_end, views_method):
    """Return (weekly_views, net_followers); either may be None if data is missing."""
    if views_method == "sum":
        weekly_views = sum(
            (r["views"] or 0) for r in rows if week_start <= r["date"] <= week_end
        )
    else:  # delta
        end_v = _value_on_or_before(rows, week_end, "views")
        prior_v = _value_on_or_before(rows, prior_end, "views")
        weekly_views = (end_v - prior_v) if (end_v is not None and prior_v is not None) else None

    end_f = _value_on_or_before(rows, week_end, "followers")
    prior_f = _value_on_or_before(rows, prior_end, "followers")
    net_followers = (end_f - prior_f) if (end_f is not None and prior_f is not None) else None
    return weekly_views, net_followers


def upsert_week(db, report_db_id, report_props, title, values):
    """Upsert one weekly row. values: {column_name: notion_value}. Schema-aware: any
    column the DB doesn't have is skipped (logged), never failing the whole write."""
    title_col = next(
        (name for name, meta in report_props.items() if meta.get("type") == "title"), None
    )

    props, skipped = {}, []
    if title_col:
        props[title_col] = {"title": [{"text": {"content": title}}]}
    for col, val in values.items():
        if col in report_props:
            props[col] = val
        else:
            skipped.append(col)
    if skipped:
        log.warning("Weekly Report columns not found, skipped: %s", ", ".join(skipped))

    existing = None
    if title_col:
        pages = db.query(
            report_db_id, {"property": title_col, "title": {"equals": title}}, page_size=1
        )
        existing = pages[0]["id"] if pages else None

    if existing:
        r = db.session.patch(
            f"{NOTION_API}/pages/{existing}", json={"properties": props}, timeout=30
        )
        action = "updated"
    else:
        r = db.session.post(
            f"{NOTION_API}/pages",
            json={"parent": {"database_id": report_db_id}, "properties": props},
            timeout=30,
        )
        action = "created"
    r.raise_for_status()
    return action
