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
        props = {
            PROP["name"]: {"title": [{"text": {"content": self._key(snapshot)}}]},
            PROP["date"]: {"date": {"start": snapshot["date"]}},
            PROP["platform"]: {"select": {"name": snapshot["platform"]}},
            PROP["account"]: {"select": {"name": snapshot["account"]}},
            PROP["followers"]: {"number": snapshot["followers"]},
            PROP["impressions"]: {"number": snapshot["impressions"]},
            PROP["engagements"]: {"number": snapshot["engagements"]},
            PROP["posts"]: {"number": snapshot["posts_published"]},
            PROP["provisional"]: {"checkbox": bool(snapshot["provisional"])},
        }
        if snapshot.get("views") is not None:
            props[PROP["views"]] = {"number": snapshot["views"]}
        if snapshot.get("followers_gained") is not None:
            props[PROP["followers_gained"]] = {"number": snapshot["followers_gained"]}
        return props

    def previous_followers(self, platform, account, before_date):
        """Follower count from the most recent snapshot strictly before before_date.

        Used to compute the daily follower delta. Returns None if there's no prior row.
        """
        resp = self.session.post(
            f"{NOTION_API}/databases/{self.database_id}/query",
            json={
                "filter": {
                    "and": [
                        {"property": PROP["platform"], "select": {"equals": platform}},
                        {"property": PROP["account"], "select": {"equals": account}},
                        {"property": PROP["date"], "date": {"before": before_date}},
                    ]
                },
                "sorts": [{"property": PROP["date"], "direction": "descending"}],
                "page_size": 1,
            },
            timeout=30,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if not results:
            return None
        return results[0]["properties"].get(PROP["followers"], {}).get("number")

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
