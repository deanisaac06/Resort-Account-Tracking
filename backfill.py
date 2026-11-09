"""One-time history backfill: pull each resort's last ~200 posts and turn them into monthly rows in
data/history/monthly.csv (same columns rollup.py writes every month), so seasonality and each
resort's long-run normal are visible right away instead of after months of tracking.

It runs inside the hourly GitHub Actions job until every account is done, picking up where it left
off each hour (progress in data/backfill/progress.json). While it's running, normal tracking is
paused (track.py checks backfill_running()). When it finishes, tracking resumes on its own.

Run:  python backfill.py            (one hour's worth of calls; called by the hourly job)
      python backfill.py --status   (print progress)
"""
import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone
from statistics import median

import rollup

HERE = os.path.dirname(os.path.abspath(__file__))
PROGRESS = os.path.join(HERE, "data", "backfill", "progress.json")
SETTINGS = os.path.join(HERE, "settings.json")

TARGET_POSTS = 200
PAGE = 50
LIGHT_FIELDS = ["timestamp", "like_count", "comments_count", "view_count", "permalink", "media_product_type"]


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=1)


def backfill_running():
    """True while the backfill has been started and isn't finished (used by track.py to pause)."""
    p = load_json(PROGRESS, None)
    return bool(p) and not p.get("completed")


# ------------------------------------------------------------------ turning posts into months
def _num(v):
    return v if isinstance(v, (int, float)) else None


def score_history(posts, s_viral, s_mega):
    """posts: compact dicts sorted oldest -> newest. Adds 'multiple' vs the account's normal at the time
    (median of the 12 earlier posts of the same type; the next 12 for the very oldest posts)."""
    out = []
    for metric in ("views", "likes"):
        seq = [p for p in posts if p["metric"] == metric]
        for i, p in enumerate(seq):
            before = [q["value"] for q in seq[max(0, i - 12):i]]
            ref = before if len(before) >= 4 else [q["value"] for q in seq[i + 1:i + 13]]
            base = median(ref) if len(ref) >= 4 else None
            mult = round(p["value"] / max(base, 1), 1) if base else None
            floor = s_viral["min_views"] if metric == "views" else s_viral["min_likes"]
            big = s_viral["absolute_views"] if metric == "views" else s_viral["absolute_likes"]
            viral = bool(mult) and ((mult >= s_viral["multiplier"] and p["value"] >= floor)
                                    or (p["value"] >= big and mult >= s_viral["absolute_min_multiple"]))
            if metric == "views":
                mega = p["value"] >= s_mega["always_views"] or (p["value"] >= s_mega["min_views"] and (mult or 0) >= s_mega["min_multiple"])
            else:
                mega = p["value"] >= s_mega["min_likes"] and (mult or 0) >= s_mega["min_multiple"]
            out.append(dict(p, multiple=mult, viral=viral, mega=mega))
    return out


def months_between(first, last):
    y, m = int(first[:4]), int(first[5:7])
    out = []
    while f"{y:04d}-{m:02d}" <= last:
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y, m + 1) if m < 12 else (y + 1, 1)
    return out


def monthly_rows(resort, handle, posts, hit_cap, now, settings):
    """One row per complete month covered by the posts (current month excluded - rollup.py does that;
    the oldest month is dropped if we stopped at the 200-post cap, since it's only partly covered)."""
    from detect import DEFAULTS, MEGA_DEFAULTS
    if not posts:
        return []
    s_viral = dict(DEFAULTS, **settings.get("viral", {}))
    s_mega = dict(MEGA_DEFAULTS, **settings.get("mega", {}))
    scored = score_history(sorted(posts, key=lambda p: p["ts"]), s_viral, s_mega)
    this_month = now.strftime("%Y-%m")
    first = min(p["ts"][:7] for p in scored)
    months = [m for m in months_between(first, this_month) if m < this_month]
    if hit_cap and months and months[0] == first:
        months = months[1:]
    rows = []
    for month in months:
        mp = [p for p in scored if p["ts"].startswith(month)]
        reels = [p["value"] for p in mp if p["metric"] == "views"]
        photos = [p["value"] for p in mp if p["metric"] == "likes"]
        top = max(mp, key=lambda p: p.get("multiple") or 0, default=None)
        rows.append({
            "month": month, "resort": resort["resort"], "handle": handle,
            "state_prov": resort.get("state_prov", ""), "tier": resort.get("tier", ""),
            "followers": "", "followers_change": "",           # not available for past months
            "posts": len(mp), "reels": len(reels),
            "median_reel_views": round(median(reels)) if reels else "",
            "total_reel_views": sum(reels) if reels else "",
            "median_photo_likes": round(median(photos)) if photos else "",
            "posts_3x": sum((p.get("multiple") or 0) >= rollup.SEMI_VIRAL for p in mp),
            "viral_posts": sum(p["viral"] for p in mp),
            "mega_posts": sum(p["mega"] for p in mp),
            "best_multiple": (top or {}).get("multiple") or "",
            "top_value": top["value"] if top else "",
            "top_metric": top["metric"] if top else "",
            "top_post": top["permalink"] if top else "",
        })
    return rows


def upsert(rows):
    """Add/replace rows in monthly.csv by (month, handle), leaving everything else alone."""
    history = rollup.read_history()
    keys = {(r["month"], r["handle"]) for r in rows}
    kept = [r for r in history if (r["month"], r["handle"]) not in keys]
    os.makedirs(os.path.dirname(rollup.HISTORY), exist_ok=True)
    with open(rollup.HISTORY, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=rollup.COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(kept + rows, key=lambda x: (x["month"], x["resort"])))


# ------------------------------------------------------------------ the hourly worker
def compact(m):
    if m.get("view_count") is not None:
        metric, value = "views", m["view_count"]
    elif m.get("like_count") is not None:
        metric, value = "likes", m["like_count"]
    else:
        return None
    return {"ts": (m.get("timestamp") or "")[:10], "metric": metric, "value": value,
            "permalink": m.get("permalink", "")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--max-calls", type=int, default=int(os.environ.get("BACKFILL_MAX_CALLS", 400)))
    args = ap.parse_args()
    now = datetime.now(timezone.utc)
    settings = load_json(SETTINGS, {})
    progress = load_json(PROGRESS, None)
    if progress is None:
        progress = {"started": now.isoformat(), "done": [], "skipped": {}, "current": None, "calls": 0}
    resorts = rollup.load_resorts()
    by_handle = {r["instagram"].strip().lstrip("@").lower(): r for r in resorts}
    todo = [h for h in by_handle if h not in progress["done"] and h not in progress["skipped"]]
    print(f"Backfill: {len(progress['done'])} done, {len(progress['skipped'])} skipped, {len(todo)} to go "
          f"({progress['calls']} API calls so far)")
    if args.status or progress.get("completed"):
        return
    w = settings.get("weekly", {})
    if now.weekday() == w.get("report_weekday", 0) and now.hour == w.get("report_hour_utc", 15):
        print("Weekly report hour - leaving the Instagram quota to the report")
        return

    from igapi import AccountUnavailable, IGClient, RateLimited, TokenError, TooMuchData
    client = IGClient()
    page = progress.get("page", PAGE)
    finished = 0
    while todo and client.calls < args.max_calls:
        cur = progress["current"]
        if not cur or cur["handle"] not in todo:
            cur = progress["current"] = {"handle": todo[0], "after": None, "posts": []}
        h = cur["handle"]
        try:
            data = client.business_discovery(h, media_limit=min(page, TARGET_POSTS - len(cur["posts"])),
                                             after=cur["after"], media_fields=LIGHT_FIELDS)
        except TooMuchData:
            page = progress["page"] = max(10, page // 2)
            print(f"  Meta asked for smaller pages - now {page} posts per call")
            continue
        except AccountUnavailable as e:
            progress["skipped"][h] = str(e)[:80]
            progress["current"] = None
            todo.remove(h)
            continue
        except RateLimited as e:
            print("  rate limited:", str(e)[:100])
            break
        except TokenError as e:
            sys.exit(f"Token error: {e}")
        except Exception as e:                     # network blip - try this account again next hour
            print(f"  @{h}: {str(e)[:120]}")
            break
        media = data.get("media") or {}
        page_posts = media.get("data", [])
        seen = {p["permalink"] for p in cur["posts"]}
        fresh = [c for c in (compact(m) for m in page_posts) if c and c["ts"] and c["permalink"] not in seen]
        cur["posts"] += fresh
        # Business Discovery often omits paging.next, so keep going whenever a cursor came back with a full page
        cursor = ((media.get("paging") or {}).get("cursors") or {}).get("after")
        asked = min(page, TARGET_POSTS - (len(cur["posts"]) - len(fresh)))
        nxt = cursor if cursor and fresh and (len(page_posts) >= asked or (media.get("paging") or {}).get("next")) else None
        cur["after"] = nxt
        hit_cap = len(cur["posts"]) >= TARGET_POSTS
        if hit_cap or not nxt:
            rows = monthly_rows(by_handle[h], h, cur["posts"], hit_cap, now, settings)
            upsert(rows)
            oldest = min((p["ts"] for p in cur["posts"]), default="-")
            print(f"  @{h}: {len(cur['posts'])} posts back to {oldest} -> {len(rows)} months")
            progress["done"].append(h)
            progress["current"] = None
            todo.remove(h)
            finished += 1
            progress["calls_at_save"] = progress["calls"] + client.calls
            save_json(PROGRESS, progress)          # don't lose finished accounts if the job is cut off
        if client.stop:
            print(f"  near Meta's hourly quota ({client.last_usage}) - continuing next hour")
            break
        client.polite_sleep(0.3)
    progress["calls"] += client.calls
    if not todo:
        progress["completed"] = now.isoformat()
        print("Backfill complete - normal tracking resumes next hour")
    save_json(PROGRESS, progress)
    print(f"This run: {client.calls} calls, {finished} accounts finished, {len(todo)} to go")


if __name__ == "__main__":
    main()
