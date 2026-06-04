"""YouTube fetcher: public counts (Data API, key) + per-day analytics (Analytics API, OAuth).

Unlike X, YouTube exposes a real per-day time series, so this writes one row PER DAY for
a trailing window and overwrites those rows each run (the maturing recent days self-correct).

Two data sources per channel:
- Data API (API key, public): current subscriber count, lifetime views, video count.
- Analytics API (owner OAuth): per-day views, thumbnail impressions, likes/comments/shares,
  subscribers gained/lost. Requires the channel owner's consent (scope yt-analytics.readonly).

Credentials are passed in by the caller (main.py reads them from the environment).
"""
import logging
from datetime import timedelta

import requests

log = logging.getLogger(__name__)

DATA_API = "https://www.googleapis.com/youtube/v3"
ANALYTICS_API = "https://youtubeanalytics.googleapis.com/v2/reports"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Core per-day metrics (one Analytics query) and impression metrics (a separate query,
# since impression metrics don't combine with the subscriber metrics in one report).
CORE_METRICS = "views,likes,comments,shares,subscribersGained,subscribersLost"
IMPRESSION_METRICS = "videoThumbnailImpressions,videoThumbnailImpressionsClickRate"


class YouTubeAuthError(Exception):
    pass


class YouTubeClient:
    def __init__(self, api_key, client_id, client_secret, session=None):
        self.api_key = api_key
        self.client_id = client_id
        self.client_secret = client_secret
        self.session = session or requests.Session()

    def refresh_access_token(self, refresh_token):
        """Google refresh-token grant. Google does NOT rotate the refresh token on use,
        so the response usually omits it; we keep the one we have."""
        resp = self.session.post(
            GOOGLE_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=30,
        )
        if resp.status_code != 200:
            raise YouTubeAuthError(f"Token refresh failed: {resp.status_code} {resp.text}")
        data = resp.json()
        return data["access_token"], data.get("refresh_token", refresh_token)

    def channel_stats(self, channel_id):
        """Current cumulative public counts via the Data API key."""
        resp = self.session.get(
            f"{DATA_API}/channels",
            params={"part": "statistics", "id": channel_id, "key": self.api_key},
            timeout=30,
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
        if not items:
            raise ValueError(f"No channel found for id {channel_id}")
        stats = items[0].get("statistics", {})
        subs = None if stats.get("hiddenSubscriberCount") else _int(stats.get("subscriberCount"))
        return {
            "subscribers": subs,
            "lifetime_views": _int(stats.get("viewCount")),
            "video_count": _int(stats.get("videoCount")),
        }

    def analytics_daily(self, access_token, channel_id, start_date, end_date, metrics):
        """Per-day report. Returns {day(str): {metric: value}}."""
        resp = self.session.get(
            ANALYTICS_API,
            headers={"Authorization": f"Bearer {access_token}"},
            params={
                "ids": f"channel=={channel_id}",
                "startDate": start_date,
                "endDate": end_date,
                "dimensions": "day",
                "metrics": metrics,
            },
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
        headers = [h["name"] for h in body.get("columnHeaders", [])]
        out = {}
        for row in body.get("rows", []):
            record = dict(zip(headers, row))
            day = record.pop("day")
            out[day] = record
        return out


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _iso(d):
    return d.strftime("%Y-%m-%d")


def fetch_channel_snapshots(client, channel, tokens, trailing_days, now, on_refresh=None):
    """Fetch one channel's trailing-window daily snapshots.

    Returns (list_of_snapshot_dicts, rotated_refresh_token_or_None). One snapshot per day
    in the trailing window. Raises on hard failure so the caller can fail soft.
    """
    refresh_token = tokens.get("refresh_token")
    state = {"access_token": tokens["access_token"], "new_refresh": None}

    def call(fn, *args):
        try:
            return fn(state["access_token"], *args)
        except requests.HTTPError as e:
            resp = getattr(e, "response", None)
            if resp is not None and resp.status_code in (401, 403) and refresh_token:
                log.info("Access token rejected for %s, refreshing", channel.get("handle"))
                new_access, new_refresh = client.refresh_access_token(refresh_token)
                state["access_token"] = new_access
                state["new_refresh"] = new_refresh
                if on_refresh:
                    on_refresh(new_access, new_refresh)
                return fn(new_access, *args)
            raise

    channel_id = channel["channel_id"]
    stats = client.channel_stats(channel_id)  # Data API key, no user token needed

    end = now.date()
    start = end - timedelta(days=trailing_days - 1)
    core = call(client.analytics_daily, channel_id, _iso(start), _iso(end), CORE_METRICS)
    # Thumbnail impressions/CTR are listed as metrics but are NOT a supported channel-report
    # query in the Analytics API, so this often 400s. Best-effort: skip impressions, keep
    # the core per-day data (views, subscribers, engagement).
    try:
        impressions = call(client.analytics_daily, channel_id, _iso(start), _iso(end), IMPRESSION_METRICS)
    except requests.HTTPError as e:
        code = getattr(getattr(e, "response", None), "status_code", "?")
        log.warning(
            "Impression metrics unavailable for %s (HTTP %s); writing rows without impressions.",
            channel.get("handle"), code,
        )
        impressions = {}

    # Walk days newest -> oldest so we can back out the cumulative subscriber count per day
    # from the current total and each day's net change.
    running_subs = stats["subscribers"]
    snapshots = []
    for offset in range(trailing_days):
        day = _iso(end - timedelta(days=offset))
        c = core.get(day, {})
        i = impressions.get(day, {})
        gained = _int(c.get("subscribersGained")) or 0
        lost = _int(c.get("subscribersLost")) or 0
        net = gained - lost
        likes = _int(c.get("likes")) or 0
        comments = _int(c.get("comments")) or 0
        shares = _int(c.get("shares")) or 0

        subs_eod = running_subs  # cumulative at end of this day
        running_subs = (running_subs - net) if running_subs is not None else None

        snapshots.append(
            {
                "platform": "YouTube",
                "account": channel.get("handle"),
                "date": day,
                "followers": subs_eod,
                "followers_gained": net,
                "views": _int(c.get("views")) or 0,
                "impressions": _int(i.get("videoThumbnailImpressions")),  # None if unavailable
                "engagements": likes + comments + shares,
                "posts_published": None,  # not tracked per-day in v2
                "provisional": True,  # whole trailing window is still settling
            }
        )

    log.info(
        "%s: subscribers=%s lifetime_views=%s days=%d",
        channel.get("handle"), stats["subscribers"], stats["lifetime_views"], len(snapshots),
    )
    return snapshots, state["new_refresh"]
