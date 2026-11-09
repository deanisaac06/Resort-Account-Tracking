"""Monthly history: one small row per resort per month, kept forever in data/history/monthly.csv.

The hourly tracker only keeps ~6 weeks of post-level data. Before a month's posts are dropped,
this rolls them up into a one-line "how did this account do this month" summary, so over time
the file shows each resort's normal, how often it beats it, and seasonality - without growing big
(about 500 rows a month, roughly 1 MB a year).

Run:  python rollup.py          (called every hour by the tracker; only does work once a month,
                                 on the 3rd, for the month before, so late views have settled)
      python rollup.py 2026-11  (roll up a specific month by hand; replaces that month's rows)
"""
import csv
import json
import os
import sys
from datetime import datetime, timezone
from statistics import median

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "data", "state.json")
RESORTS = os.path.join(HERE, "resorts.csv")
HISTORY = os.path.join(HERE, "data", "history", "monthly.csv")
ROLLUP_DAY = 3          # day of the month the previous month is rolled up
SEMI_VIRAL = 3.0        # "beat its normal" mark: 3x the account's normal (same as the weekly report)

COLUMNS = [
    "month", "resort", "handle", "state_prov", "tier",
    "followers", "followers_change",
    "posts", "reels",
    "median_reel_views", "total_reel_views", "median_photo_likes",
    "posts_3x", "viral_posts", "mega_posts",
    "best_multiple", "top_value", "top_metric", "top_post",
]


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default


def read_history():
    if not os.path.exists(HISTORY):
        return []
    with open(HISTORY, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def prev_month(now):
    y, m = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
    return f"{y:04d}-{m:02d}"


def rollup(month, state, resorts, history):
    """One row per tracked resort for `month` (YYYY-MM)."""
    posts_by = {}
    for p in state.get("posts", {}).values():
        if p.get("posted", "").startswith(month):
            posts_by.setdefault(p["handle"], []).append(p)
    last_followers = {}
    for row in history:                       # most recent earlier month's follower count per handle
        if row["month"] < month and row.get("followers"):
            last_followers[row["handle"]] = int(row["followers"])

    rows = []
    for r in resorts:
        h = r["instagram"].strip().lstrip("@").lower()
        acct = state.get("accounts", {}).get(h, {})
        if not acct.get("followers") and not posts_by.get(h):
            continue                          # never read successfully - nothing to say about it
        posts = posts_by.get(h, [])
        reels = [p["value"] for p in posts if p["metric"] == "views"]
        photos = [p["value"] for p in posts if p["metric"] == "likes"]
        top = max(posts, key=lambda p: p.get("multiple") or 0, default=None)
        followers = acct.get("followers")
        change = followers - last_followers[h] if followers is not None and h in last_followers else ""
        rows.append({
            "month": month, "resort": r["resort"], "handle": h,
            "state_prov": r.get("state_prov", ""), "tier": r.get("tier", ""),
            "followers": followers if followers is not None else "", "followers_change": change,
            "posts": len(posts), "reels": len(reels),
            "median_reel_views": round(median(reels)) if reels else "",
            "total_reel_views": sum(reels) if reels else "",
            "median_photo_likes": round(median(photos)) if photos else "",
            "posts_3x": sum((p.get("multiple") or 0) >= SEMI_VIRAL for p in posts),
            "viral_posts": sum(bool(p.get("viral")) for p in posts),
            "mega_posts": sum(bool(p.get("mega_alerted")) for p in posts),
            "best_multiple": (top or {}).get("multiple") or "",
            "top_value": top["value"] if top else "",
            "top_metric": top["metric"] if top else "",
            "top_post": top["permalink"] if top else "",
        })
    return rows


def write(month, rows, history):
    kept = [row for row in history if row["month"] != month]
    os.makedirs(os.path.dirname(HISTORY), exist_ok=True)
    with open(HISTORY, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(kept + rows, key=lambda x: (x["month"], x["resort"])))


def load_resorts():
    with open(RESORTS, newline="", encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if r.get("instagram")]


def main():
    now = datetime.now(timezone.utc)
    history = read_history()
    if len(sys.argv) > 1:
        month = sys.argv[1]
    else:
        month = prev_month(now)
        # done = rows with follower counts exist (backfilled months have none, so they get replaced)
        if now.day < ROLLUP_DAY or any(row["month"] == month and row.get("followers") for row in history):
            return                            # not time yet, or already done
    state = load_json(STATE, {})
    rows = rollup(month, state, load_resorts(), history)
    if not rows:
        print(f"No tracked accounts with data for {month} - nothing to roll up")
        return
    write(month, rows, history)
    print(f"Rolled up {month}: {len(rows)} resorts, {sum(r['posts'] for r in rows)} posts, "
          f"{sum(r['posts_3x'] for r in rows)} posts at 3x+ normal")


if __name__ == "__main__":
    main()
