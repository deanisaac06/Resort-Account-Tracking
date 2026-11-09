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


# --------------------------------------------------------------------------- mega viral
# "Industry-wide notable": rare enough that it would get talked about across ski marketing.
# Judgment call: a typical resort Reel lands in the 5K-150K view range, and a 1M+ Reel from a
# resort account happens only a handful of times a month across North America. Requiring 2M+
# views AND 10x the account's own normal keeps this to a few alerts per season. Anything past
# 5M views alerts regardless (that's top-of-the-industry for any resort account).
MEGA_DEFAULTS = {
    "min_views": 2_000_000,     # views needed (together with min_multiple)
    "min_multiple": 10.0,       # ...and at least this many times the account's median
    "always_views": 5_000_000,  # alert at this many views no matter what
    "min_likes": 100_000,       # photo posts (no view counts): likes needed, with min_multiple
    "max_age_days": 30,
    "watch_views": 300_000,     # posts climbing past this get re-checked every run
    "watch_multiple": 8.0,      # ...or posts already at this multiple
}


def score_posts(posts, followers=None, now=None, max_age_days=30, min_baseline_posts=4):
    """Every recent post with its metric, the account's normal for that metric, and the multiple."""
    now = now or datetime.now(timezone.utc)
    out = []
    for post in posts:
        name, value = metric_for(post)
        if value is None or not post.get("timestamp"):
            continue
        age_days = (now - _parse_ts(post["timestamp"])).total_seconds() / 86400
        if age_days > max_age_days:
            continue
        base = baselines([p for p in posts if p.get("id") != post.get("id")])
        base_val, n_base = base[name], base["n_" + name]
        multiple = value / max(base_val, 1) if base_val and n_base >= min_baseline_posts else None
        out.append({
            "media_id": post.get("id"),
            "permalink": post.get("permalink"),
            "timestamp": post.get("timestamp"),
            "posted": post.get("timestamp", "")[:10],
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
            "caption": (post.get("caption") or "").replace("\n", " ")[:200],
            "thumb": post.get("thumbnail_url") or post.get("media_url"),
        })
    return out


def is_mega(p, settings=None):
    s = dict(MEGA_DEFAULTS, **(settings or {}))
    if p.get("age_days", 0) > s["max_age_days"]:
        return False
    mult = p.get("multiple") or 0
    if p["metric"] == "views":
        return p["value"] >= s["always_views"] or (p["value"] >= s["min_views"] and mult >= s["min_multiple"])
    return p["value"] >= s["min_likes"] and mult >= s["min_multiple"]


def is_watch(p, settings=None):
    """Posts that are climbing fast enough to be worth re-checking every hour."""
    s = dict(MEGA_DEFAULTS, **(settings or {}))
    return p.get("age_days", 99) <= 7 and (
        (p["metric"] == "views" and p["value"] >= s["watch_views"]) or (p.get("multiple") or 0) >= s["watch_multiple"])
