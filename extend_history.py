"""Deeper history for chosen resorts: keep paging each account's posts back to a target date
(default Jan 2021, Major-tier resorts) and add the older months to data/history/monthly.csv.

The first backfill (backfill.py) stopped at 300-1000 posts per account, which for busy resorts only
reaches 2023-2024. This picks up those accounts and keeps going until it reaches `since`.

It runs after the normal tracker each hour and only uses spare Instagram quota: it stops once Meta
reports `usage_ceiling` % of the hourly quota used (default 60%, the tracker stops at 85%), so viral
tracking and alerts keep running the whole time. It finishes on its own and then does nothing.

Only months older than what monthly.csv already has for an account are added; existing rows
are never changed.

Settings (settings.json -> "history_extend"):
  since          "2021-01"   go back to this month
  tiers          ["Major"]   which tiers to extend (add "Mid" / "Small" later if wanted)
  max_posts      3000        safety cap per account
  usage_ceiling  60          % of Meta's hourly quota to stop at
  max_calls      300         API calls per hour at most

Run:  python extend_history.py            (one hour's worth; called by the hourly job)
      python extend_history.py --status   (print progress)
      python extend_history.py --dry-run --only jacksonhole   (pull one account, print, write nothing)
"""
import argparse
import os
import sys
from datetime import datetime, timezone

import rollup
from backfill import (LIGHT_FIELDS, PAGE, PROGRESS as FIRST_PROGRESS, SETTINGS, compact,
                      load_json, monthly_rows, save_json, upsert)

HERE = os.path.dirname(os.path.abspath(__file__))
PROGRESS = os.path.join(HERE, "data", "backfill", "extend.json")
DEFAULTS = {"since": "2021-01", "tiers": ["Major"], "max_posts": 3000, "usage_ceiling": 60, "max_calls": 300}


def first_months():
    """{handle: earliest month with posts already in monthly.csv}"""
    out = {}
    for r in rollup.read_history():
        if str(r.get("posts") or "0") not in ("", "0"):
            h = r["handle"]
            if h not in out or r["month"] < out[h]:
                out[h] = r["month"]
    return out


def pick_accounts(resorts, cfg, first, skipped):
    """Accounts in the chosen tiers whose history starts after `since` (and weren't unreadable)."""
    tiers = {t.lower() for t in cfg["tiers"]}
    out = []
    for r in resorts:
        h = r["instagram"].strip().lstrip("@").lower()
        if (r.get("tier") or "").lower() in tiers and h in first and first[h] > cfg["since"] and h not in skipped:
            out.append(h)
    return sorted(set(out), key=lambda h: first[h], reverse=True)   # shortest history first


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only")
    args = ap.parse_args()
    now = datetime.now(timezone.utc)
    settings = load_json(SETTINGS, {})
    cfg = dict(DEFAULTS, **settings.get("history_extend", {}))
    first_run = load_json(FIRST_PROGRESS, {})
    if first_run and not first_run.get("completed"):
        print("First backfill still running - deeper history waits for it")
        return

    resorts = rollup.load_resorts()
    by_handle = {r["instagram"].strip().lstrip("@").lower(): r for r in resorts}
    first = first_months()
    progress = load_json(PROGRESS, None)
    if progress is None or progress.get("since") != cfg["since"] or progress.get("tiers") != cfg["tiers"]:
        # new or changed target -> (re)build the to-do list, keeping accounts already finished
        old_done = (progress or {}).get("done", {})
        progress = {"started": now.isoformat(), "since": cfg["since"], "tiers": cfg["tiers"],
                    "todo": pick_accounts(resorts, cfg, first, first_run.get("skipped", {})),
                    "done": old_done, "skipped": {}, "current": None, "calls": 0}
        progress["todo"] = [h for h in progress["todo"] if h not in old_done]
        progress.pop("completed", None)
    todo = [args.only.lower()] if args.only else progress["todo"]
    print(f"Deeper history (back to {cfg['since']}, tiers {', '.join(cfg['tiers'])}): "
          f"{len(progress['done'])} done, {len(todo)} to go, {progress['calls']} API calls so far")
    if args.status or (progress.get("completed") and not args.only):
        return
    w = settings.get("weekly", {})
    if now.weekday() == w.get("report_weekday", 0) and now.hour == w.get("report_hour_utc", 15) and not args.only:
        print("Weekly report hour - leaving the Instagram quota to the report")
        return

    from igapi import AccountUnavailable, IGClient, RateLimited, TokenError, TooMuchData
    client = IGClient(usage_ceiling=cfg["usage_ceiling"])
    page = progress.get("page", PAGE)
    since_day = cfg["since"] + "-01"
    while todo and client.calls < cfg["max_calls"]:
        cur = progress["current"] if not args.only else None
        if not cur or cur["handle"] not in todo:
            cur = {"handle": todo[0], "after": None, "posts": []}
            if not args.only:
                progress["current"] = cur
        h = cur["handle"]
        try:
            data = client.business_discovery(h, media_limit=page, after=cur["after"], media_fields=LIGHT_FIELDS)
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
        except Exception as e:                     # network blip - try again next hour
            print(f"  @{h}: {str(e)[:120]}")
            break
        media = data.get("media") or {}
        page_posts = media.get("data", [])
        seen = {p["permalink"] for p in cur["posts"]}
        fresh = [c for c in (compact(m) for m in page_posts) if c and c["ts"] and c["permalink"] not in seen]
        cur["posts"] += fresh
        cursor = ((media.get("paging") or {}).get("cursors") or {}).get("after")
        nxt = cursor if cursor and fresh and (len(page_posts) >= page or (media.get("paging") or {}).get("next")) else None
        cur["after"] = nxt
        oldest = min((p["ts"] for p in cur["posts"]), default="9999")
        reached = oldest < since_day
        capped = len(cur["posts"]) >= cfg["max_posts"]
        if reached or capped or not nxt:
            # the oldest month is only partly covered unless we ran out of posts
            rows = monthly_rows(by_handle[h], h, cur["posts"], reached or capped, now, settings)
            keep_before = first.get(h, "9999")
            rows = [r for r in rows if r["month"] < keep_before]
            why = "reached target" if reached else ("hit cap" if capped else "no older posts")
            print(f"  @{h}: {len(cur['posts'])} posts back to {oldest} ({why}) -> {len(rows)} older months "
                  f"(had from {keep_before})")
            if not args.dry_run:
                if rows:
                    upsert(rows)
                progress["done"][h] = {"posts": len(cur["posts"]), "oldest": oldest, "months_added": len(rows)}
                progress["current"] = None
            todo.remove(h)
            if not args.dry_run:
                progress["calls_at_save"] = progress["calls"] + client.calls
                save_json(PROGRESS, progress)
            if args.only:
                break
        if client.stop:
            print(f"  used {cfg['usage_ceiling']}% of Meta's hourly quota ({client.last_usage}) - continuing next hour")
            break
        client.polite_sleep(0.3)
    if args.dry_run:
        print(f"Dry run: {client.calls} calls, nothing written")
        return
    progress["calls"] += client.calls
    if not progress["todo"]:
        progress["completed"] = now.isoformat()
        print("Deeper history complete")
    save_json(PROGRESS, progress)
    print(f"This run: {client.calls} calls, {len(progress['todo'])} accounts to go")


if __name__ == "__main__":
    main()
