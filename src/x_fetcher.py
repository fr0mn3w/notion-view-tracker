"""X (Twitter) owned-reads fetcher.

Pulls account-level metrics for a team-owned account using OAuth2 user-context auth,
which is required to read non_public_metrics (impressions) on the account's own posts.

One run per account: one /users/me call + one (usually single-page) timeline pull,
capped by max_posts. X is pay-per-use, so calls are kept minimal.

This module is transport + aggregation only. Credentials are passed in by the caller
(main.py reads them from the environment); nothing here is hardcoded, and no auth flow
runs on its own.
"""
import logging
from datetime import timedelta

import requests

log = logging.getLogger(__name__)

X_API = "https://api.twitter.com/2"
TOKEN_URL = "https://api.twitter.com/2/oauth2/token"

# public_metrics: like/retweet/reply/quote/bookmark/impression counts.
# non_public_metrics: owner-only impression_count (preferred when present).
TWEET_FIELDS = "created_at,public_metrics,non_public_metrics"


class XAuthError(Exception):
    pass


class XClient:
    """Thin transport wrapper around the X v2 API."""

    def __init__(self, client_id, client_secret, session=None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.session = session or requests.Session()

    def refresh_access_token(self, refresh_token):
        """OAuth2 refresh-token grant. Confidential client -> HTTP Basic auth.

        Returns (access_token, refresh_token). X rotates the refresh token on each
        use, so the returned refresh_token may differ from the one passed in.
        """
        resp = self.session.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self.client_id,
            },
            auth=(self.client_id, self.client_secret),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        if resp.status_code != 200:
            raise XAuthError(f"Token refresh failed: {resp.status_code} {resp.text}")
        data = resp.json()
        return data["access_token"], data.get("refresh_token", refresh_token)

    def _get(self, access_token, path, params=None):
        resp = self.session.get(
            f"{X_API}{path}",
            headers={"Authorization": f"Bearer {access_token}"},
            params=params or {},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def get_me(self, access_token):
        body = self._get(
            access_token, "/users/me", {"user.fields": "public_metrics,username"}
        )
        return body["data"]

    def get_user_posts(self, access_token, user_id, start_time, max_posts):
        posts = []
        params = {
            "max_results": min(100, max_posts),
            "tweet.fields": TWEET_FIELDS,
            "start_time": start_time,
            "exclude": "retweets,replies",  # original published posts only
        }
        next_token = None
        while len(posts) < max_posts:
            if next_token:
                params["pagination_token"] = next_token
            body = self._get(access_token, f"/users/{user_id}/tweets", params)
            posts.extend(body.get("data", []))
            next_token = body.get("meta", {}).get("next_token")
            if not next_token:
                break
        return posts[:max_posts]


def summarize_posts(posts):
    totals = {
        "impressions": 0,
        "likes": 0,
        "reposts": 0,
        "replies": 0,
        "quotes": 0,
        "bookmarks": 0,
    }
    for p in posts:
        pm = p.get("public_metrics") or {}
        npm = p.get("non_public_metrics") or {}
        totals["impressions"] += npm.get("impression_count") or pm.get("impression_count") or 0
        totals["likes"] += pm.get("like_count", 0)
        totals["reposts"] += pm.get("retweet_count", 0)
        totals["replies"] += pm.get("reply_count", 0)
        totals["quotes"] += pm.get("quote_count", 0)
        totals["bookmarks"] += pm.get("bookmark_count", 0)
    return totals


def _iso_z(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_account_snapshot(client, account, tokens, window_days, max_posts, now):
    """Fetch one account's daily snapshot.

    Returns (snapshot_dict, rotated_refresh_token_or_None). Raises on hard failure
    so the caller can fail soft (skip this account, leave its rows intact).
    """
    refresh_token = tokens.get("refresh_token")
    state = {"access_token": tokens["access_token"], "new_refresh": None}

    def call(fn, *args):
        try:
            return fn(state["access_token"], *args)
        except requests.HTTPError as e:
            resp = getattr(e, "response", None)
            if resp is not None and resp.status_code == 401 and refresh_token:
                log.info("Access token rejected for @%s, refreshing", account.get("handle"))
                new_access, new_refresh = client.refresh_access_token(refresh_token)
                state["access_token"] = new_access
                state["new_refresh"] = new_refresh
                return fn(new_access, *args)
            raise

    me = call(client.get_me)
    user_id = me["id"]
    followers = (me.get("public_metrics") or {}).get("followers_count", 0)

    start_time = _iso_z(now - timedelta(days=window_days))
    posts = call(client.get_user_posts, user_id, start_time, max_posts)

    totals = summarize_posts(posts)
    today = now.date().isoformat()
    posts_today = sum(1 for p in posts if (p.get("created_at") or "")[:10] == today)
    engagements = (
        totals["likes"] + totals["reposts"] + totals["replies"]
        + totals["quotes"] + totals["bookmarks"]
    )

    snapshot = {
        "platform": "X",
        "account": me.get("username") or account.get("handle"),
        "date": today,
        "followers": followers,
        "views": None,  # X has no separate "views"; impressions carries the reach metric
        "impressions": totals["impressions"],
        "engagements": engagements,
        "posts_published": posts_today,
        "provisional": True,  # current snapshot is still maturing
    }
    log.info(
        "@%s: followers=%d posts_in_window=%d impressions=%d engagements=%d posts_today=%d",
        snapshot["account"], followers, len(posts),
        totals["impressions"], engagements, posts_today,
    )
    return snapshot, state["new_refresh"]
