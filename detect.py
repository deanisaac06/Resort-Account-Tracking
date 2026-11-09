"""Decide whether a post is 'viral' relative to the account that posted it.

A post is flagged when it clearly outperforms that account's own normal, or when
it is big in absolute terms. Tune the numbers in settings.json.
"""
from datetime import datetime, timezone
from statistics import median

DEFAULTS = {
    "multiplier": 5.0,            # post must beat the account's median by this many times...
    "min_views": 25000,           # ...and also clear this floor (views)
    "min_likes": 2500,            # ...or this floor (likes, for posts without view counts)
    "absolute_views": 500000,     # any post above this is flagged regardless of baseline
    "absolute_likes": 25000,
    "absolute_min_multiple": 1.5, # big accounts: absolute hits must still beat their own median by 1.5x
    "views_per_follower": 3.0,    # or views >= 3x the account's follower count (and >= min_views)...
    "vpf_min_multiple": 2.0,      # ...as long as it's also 2x the account's own median
    "max_age_days": 21,           # only flag recent posts
    "min_baseline_posts": 4,      # need this many other posts to compute a baseline
}


def _parse_ts(ts):
    # Graph API format: 2026-11-08T17:04:12+0000
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z")


def metric_for(post):
    """Return (metric_name, value) - views when Instagram reports them, else likes."""
    if post.get("view_count") is not None:
        return "views", post["view_count"]
    if post.get("like_count") is not None:
        return "likes", post["like_count"]
    return None, None


def baselines(posts):
    views = [p["view_count"] for p in posts if p.get("view_count") is not None]
    likes = [p["like_count"] for p in posts if p.get("like_count") is not None]
    return {
        "views": median(views) if views else None,
        "likes": median(likes) if likes else None,
        "n_views": len(views),
        "n_likes": len(likes),
    }


def find_viral(posts, followers=None, settings=None, now=None):
    s = dict(DEFAULTS, **(settings or {}))
    now = now or datetime.now(timezone.utc)
    hits = []
    for post in posts:
        name, value = metric_for(post)
        if value is None or not post.get("timestamp"):
            continue
        age_days = (now - _parse_ts(post["timestamp"])).total_seconds() / 86400
        if age_days > s["max_age_days"]:
            continue
        others = [p for p in posts if p.get("id") != post.get("id")]
        base = baselines(others)
        base_val = base[name]
        n_base = base["n_" + name]
        floor = s["min_views"] if name == "views" else s["min_likes"]
        absolute = s["absolute_views"] if name == "views" else s["absolute_likes"]

        reasons = []
        multiple = None
        if base_val and n_base >= s["min_baseline_posts"]:
            multiple = value / max(base_val, 1)
            if multiple >= s["multiplier"] and value >= floor:
                reasons.append(f"{multiple:.1f}x the account's median {name}")
        if value >= absolute and (multiple is None or multiple >= s["absolute_min_multiple"]):
            reasons.append(f"over {absolute:,} {name}")
        if name == "views" and followers and value >= floor and value / followers >= s["views_per_follower"] \
                and (multiple is None or multiple >= s["vpf_min_multiple"]):
            reasons.append(f"{value / followers:.1f} views per follower")
        if reasons:
            hits.append({
                "media_id": post.get("id"),
                "permalink": post.get("permalink"),
                "timestamp": post.get("timestamp"),
                "age_days": round(age_days, 1),
                "media_type": post.get("media_product_type") or post.get("media_type"),
                "metric": name,
                "value": value,
                "views": post.get("view_count"),
                "likes": post.get("like_count"),
                "comments": post.get("comments_count"),
                "baseline": round(base_val) if base_val else None,
                "multiple": round(multiple, 1) if multiple else None,
                "followers": followers,
                "reason": "; ".join(reasons),
                "caption": (post.get("caption") or "").replace("\n", " ")[:200],
            })
    return hits
