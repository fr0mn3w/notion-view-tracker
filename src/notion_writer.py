"""Notion writer: upsert daily snapshot rows into a single database.

Upsert key is (platform, account, date), encoded into the row's title so re-runs
and trailing re-fetches overwrite cleanly instead of duplicating.

Property names below MUST match the Notion database exactly (see README setup).
"""
import logging

import requests

log = logging.getLogger(__name__)

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# Canonical property name per logical field (used for query filters).
PROP = {
    "name": "Name",  # title property, holds the unique upsert key
    "date": "Date",
    "platform": "Platform",
    "account": "Account",
    "followers": "Followers or Subscribers",
    "followers_gained": "Followers Gained",
    "views": "Views",
    "impressions": "Impressions or Reach",
    "engagements": "Engagements",
    "posts": "Posts Published",
    "provisional": "Provisional",
}

# Acceptable column names per field, in priority order. The writer uses whichever
# one exists in the database, so common renames (e.g. "Impressions or Reach" -> "Views",
# "Followers or Subscribers" -> "Followers") keep working without code changes.
CANDIDATES = {
    "name": ["Name"],
    "date": ["Date"],
    "platform": ["Platform"],
    "account": ["Account"],
    "followers": ["Followers or Subscribers", "Followers", "Subscribers"],
    "followers_gained": ["Followers Gained", "Followers Δ", "Net Followers"],
    "views": ["Views"],
    "impressions": ["Impressions or Reach", "Impressions", "Reach", "Views"],
    "engagements": ["Engagements"],
    "posts": ["Posts Published", "Posts"],
    "provisional": ["Provisional"],
}


class NotionWriter:
    def __init__(self, token, database_id, session=None):
        self.database_id = database_id
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            }
        )
        self._schema_cache = None

    def _existing_props(self):
        """Set of property names that actually exist in the database (cached per run).

        Lets the writer adapt to columns the user renamed or removed instead of
        crashing the whole row write on one missing property.
        """
        if self._schema_cache is None:
            resp = self.session.get(
                f"{NOTION_API}/databases/{self.database_id}", timeout=30
            )
            resp.raise_for_status()
            self._schema_cache = set(resp.json().get("properties", {}).keys())
        return self._schema_cache

    def _resolve(self, field, used=()):
        """First acceptable column name for a logical field that exists in the DB
        and hasn't already been claimed. Returns None if none match."""
        for name in CANDIDATES.get(field, [PROP.get(field)]):
            if name and name in self._existing_props() and name not in used:
                return name
        return None

    @staticmethod
    def _key(snapshot):
        return f'{snapshot["date"]}|{snapshot["platform"]}|{snapshot["account"]}'

    def _find_page(self, key):
        resp = self.session.post(
            f"{NOTION_API}/databases/{self.database_id}/query",
            json={
                "filter": {"property": PROP["name"], "title": {"equals": key}},
                "page_size": 1,
            },
            timeout=30,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        return results[0]["id"] if results else None

    def _properties(self, snapshot):
        # logical field -> Notion value object (only fields with a value to write)
        values = {
            "name": {"title": [{"text": {"content": self._key(snapshot)}}]},
            "date": {"date": {"start": snapshot["date"]}},
            "platform": {"select": {"name": snapshot["platform"]}},
            "account": {"select": {"name": snapshot["account"]}},
            "followers": {"number": snapshot["followers"]},
            "impressions": {"number": snapshot["impressions"]},
            "engagements": {"number": snapshot["engagements"]},
            "posts": {"number": snapshot["posts_published"]},
            "provisional": {"checkbox": bool(snapshot["provisional"])},
        }
        if snapshot.get("views") is not None:
            values["views"] = {"number": snapshot["views"]}
        if snapshot.get("followers_gained") is not None:
            values["followers_gained"] = {"number": snapshot["followers_gained"]}

        # Map each field to whatever column name actually exists in the DB. A field
        # with no matching column is skipped (logged) rather than failing the row.
        props, used, missing = {}, set(), []
        for field, value in values.items():
            name = self._resolve(field, used)
            if name:
                props[name] = value
                used.add(name)
            else:
                missing.append(field)
        if missing:
            log.warning("No Notion column found for: %s (skipped)", ", ".join(missing))
        return props

    def previous_followers(self, platform, account, before_date):
        """Follower count from the most recent snapshot strictly before before_date.

        Used to compute the daily follower delta. Returns None if there's no prior row.
        """
        followers_col = self._resolve("followers")
        if not followers_col:
            return None
        resp = self.session.post(
            f"{NOTION_API}/databases/{self.database_id}/query",
            json={
                "filter": {
                    "and": [
                        {"property": self._resolve("platform") or PROP["platform"], "select": {"equals": platform}},
                        {"property": self._resolve("account") or PROP["account"], "select": {"equals": account}},
                        {"property": self._resolve("date") or PROP["date"], "date": {"before": before_date}},
                    ]
                },
                "sorts": [{"property": self._resolve("date") or PROP["date"], "direction": "descending"}],
                "page_size": 1,
            },
            timeout=30,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if not results:
            return None
        return results[0]["properties"].get(followers_col, {}).get("number")

    def upsert(self, snapshot):
        key = self._key(snapshot)
        page_id = self._find_page(key)
        props = self._properties(snapshot)
        if page_id:
            resp = self.session.patch(
                f"{NOTION_API}/pages/{page_id}",
                json={"properties": props},
                timeout=30,
            )
            action = "updated"
        else:
            resp = self.session.post(
                f"{NOTION_API}/pages",
                json={"parent": {"database_id": self.database_id}, "properties": props},
                timeout=30,
            )
            action = "created"
        resp.raise_for_status()
        log.info("Notion row %s: %s", action, key)
        return action

    def age_out_provisional(self, platform, cutoff_date):
        """Clear the Provisional flag on settled rows (date before cutoff_date).

        Costs no X calls. Keeps the still-maturing window flagged without touching
        the freshly written rows.
        """
        resp = self.session.post(
            f"{NOTION_API}/databases/{self.database_id}/query",
            json={
                "filter": {
                    "and": [
                        {"property": PROP["platform"], "select": {"equals": platform}},
                        {"property": PROP["provisional"], "checkbox": {"equals": True}},
                        {"property": PROP["date"], "date": {"before": cutoff_date}},
                    ]
                }
            },
            timeout=30,
        )
        resp.raise_for_status()
        pages = resp.json().get("results", [])
        for page in pages:
            self.session.patch(
                f"{NOTION_API}/pages/{page['id']}",
                json={"properties": {PROP["provisional"]: {"checkbox": False}}},
                timeout=30,
            )
        if pages:
            log.info("Aged out Provisional on %d %s row(s)", len(pages), platform)
