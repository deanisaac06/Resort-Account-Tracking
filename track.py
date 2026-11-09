"""Check a rotating batch of resort Instagram accounts and flag viral posts.

Run:  python track.py            (normal run, used by GitHub Actions)
      python track.py --dry-run  (no sheet/email, prints hits)

Each run checks up to MAX_CALLS accounts (Business Discovery allows ~200 calls/hour
per app user), picking up where the last run stopped, so the whole list is covered
every few hours. Accounts with a post that's climbing fast (see is_watch in detect.py)
are re-checked at the start of every run so a mega-viral post is caught within ~an hour.

Emails: only "mega viral" posts are emailed from here, the moment they're found.
The weekly performance report is sent by weekly.py.
"""
import argparse
import csv
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from statistics import median

from detect import find_viral, is_mega, is_watch, score_posts
from igapi import AccountUnavailable, IGClient, RateLimited, TokenError
import outputs

HERE = os.path.dirname(os.path.abspath(__file__))
RESORTS = os.path.join(HERE, "resorts.csv")
STATE = os.path.join(HERE, "data", "state.json")
VIRAL_LOG = os.path.join(HERE, "data", "viral_posts.csv")
ACCOUNTS_OUT = os.path.join(HERE, "data", "accounts.csv")
SETTINGS = os.path.join(HERE, "settings.json")


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def load_resorts():
    with open(RESORTS, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    tracked = [r for r in rows if r.get("instagram") and r.get("track", "yes").strip().lower() in ("yes", "y", "1", "true")]
    # Major resorts first so they're always early in each cycle
    order = {"Major": 0, "Mid": 1, "Small": 2}
    tracked.sort(key=lambda r: (order.get(r.get("tier"), 3), r["resort"]))
    return tracked


def append_csv(path, rows, columns):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-calls", type=int, default=int(os.environ.get("MAX_CALLS", 150)))
    ap.add_argument("--only", help="check a single handle (for testing)")
    args = ap.parse_args()

    settings = load_json(SETTINGS, {})
    state = load_json(STATE, {"cursor": 0, "accounts": {}, "alerted": {}})
    resorts = load_resorts()
    if args.only:
        resorts = [r for r in resorts if r["instagram"].lower() == args.only.lower()] or [
            {"resort": args.only, "instagram": args.only, "state_prov": "", "country": "", "tier": ""}]
    if not resorts:
        sys.exit("No accounts to track - fill the instagram column in resorts.csv")

    now = datetime.now(timezone.utc)
    w = settings.get("weekly", {})
    if not args.only and now.weekday() == w.get("report_weekday", 0) and now.hour == w.get("report_hour_utc", 15):
        print("Weekly report hour - skipping tracking so the report has Instagram quota to work with.")
        return
    client = IGClient()
    now_s = now.strftime("%Y-%m-%d %H:%M UTC")
    state.setdefault("posts", {})
    cursor = 0 if args.only else state.get("cursor", 0) % len(resorts)
    by_handle = {r["instagram"].strip().lstrip("@").lower(): r for r in resorts}

    # 1) watchlist: accounts with a fast-climbing post from the last week, biggest first
    watch = []
    if not args.only:
        climbing = sorted((p for p in state["posts"].values() if is_watch(p, settings.get("mega"))),
                          key=lambda p: -p["value"])
        for p in climbing:
            if p["handle"] in by_handle and p["handle"] not in watch:
                watch.append(p["handle"])
        watch = watch[: settings.get("watch_max", 20)]
    # 2) then the normal rotation through the whole list
    rotation = [resorts[(cursor + i) % len(resorts)]["instagram"].strip().lstrip("@").lower()
                for i in range(len(resorts))]
    queue = watch + [h for h in rotation if h not in watch]

    new_hits, new_mega, checked, rotated, stop_reason = [], [], 0, 0, None
    for handle in queue[: args.max_calls]:
        r = by_handle[handle]
        in_rotation = handle not in watch
        acct = state["accounts"].setdefault(handle, {})
        try:
            data = client.business_discovery(handle, media_limit=settings.get("posts_per_account", 12))
        except AccountUnavailable as e:
            acct.update(status="unavailable: " + str(e)[:80], last_checked=now_s)
            checked += 1
            rotated += in_rotation
            continue
        except RateLimited as e:
            stop_reason = f"rate limit: {e}"
            break
        except TokenError as e:
            outputs.send_email("Ski tracker: Instagram token problem", f"The access token was rejected:\n{e}\n\nGenerate a new one and update the IG_ACCESS_TOKEN secret.")
            sys.exit(f"Token error: {e}")
        except Exception as e:  # network blip etc. - skip this account this round
            acct.update(status="error: " + str(e)[:80], last_checked=now_s)
            checked += 1
            rotated += in_rotation
            continue

        checked += 1
        rotated += in_rotation
        posts = (data.get("media") or {}).get("data", [])
        followers = data.get("followers_count")
        views = [p["view_count"] for p in posts if p.get("view_count") is not None]
        likes = [p["like_count"] for p in posts if p.get("like_count") is not None]
        acct.update(
            status="ok", last_checked=now_s, followers=followers, posts_total=data.get("media_count"),
            median_views=round(median(views)) if views else None,
            median_likes=round(median(likes)) if likes else None,
            last_post=posts[0]["timestamp"][:10] if posts else None,
            website=data.get("website"),
        )
        meta = dict(resort=r["resort"], handle=handle, state_prov=r.get("state_prov", ""), tier=r.get("tier", ""))

        # keep every recent post's latest numbers (feeds the weekly report)
        for p in score_posts(posts, followers, now):
            rec = state["posts"].setdefault(p["media_id"], {"first_seen": now_s})
            thumb = p.pop("thumb", None)
            rec.update(p, **meta, last_seen=now_s)
            if thumb and (p.get("multiple") or 0) >= 2.5:   # only keep preview links for standouts
                rec["thumb"] = thumb
            if is_mega(rec, settings.get("mega")) and not rec.get("mega_alerted"):
                rec["mega_alerted"] = now_s
                new_mega.append(dict(rec))

        # regular "viral" flags (5x normal etc.) -> sheet + log + weekly report
        for hit in find_viral(posts, followers, settings.get("viral", {}), now):
            if hit["media_id"] in state["posts"]:
                state["posts"][hit["media_id"]]["viral"] = hit["reason"]
            if hit["media_id"] in state["alerted"]:
                continue
            hit.update(detected_at=now_s, **meta, posted=hit["timestamp"][:10])
            state["alerted"][hit["media_id"]] = {"handle": handle, "at": now_s, "value": hit["value"]}
            new_hits.append(hit)
        if client.stop:
            stop_reason = f"near Meta's hourly quota ({client.last_usage}) - continuing next run"
            break
        client.polite_sleep(settings.get("sleep_seconds", 0.5))

    # forget posts older than ~6 weeks so state.json stays small (rollup.py has saved each month by then)
    cutoff = (now - timedelta(days=settings.get("keep_post_days", 45))).strftime("%Y-%m-%d")
    for k in [k for k, p in state["posts"].items() if p.get("posted", "9999") < cutoff]:
        state["posts"].pop(k)

    if not args.only:
        state["cursor"] = (cursor + rotated) % len(resorts)
    state["last_run"] = {"at": now_s, "checked": checked, "watchlist": len(watch), "api_calls": client.calls,
                         "new_hits": len(new_hits), "new_mega": len(new_mega), "stopped": stop_reason,
                         "usage": client.last_usage}

    # trim alert memory to the last ~60 days so state.json stays small
    if len(state["alerted"]) > 5000:
        for k in sorted(state["alerted"], key=lambda k: state["alerted"][k]["at"])[:-5000]:
            state["alerted"].pop(k)

    accounts = []
    for r in load_resorts():
        a = state["accounts"].get(r["instagram"].strip().lstrip("@").lower(), {})
        accounts.append({**{k: r.get(k, "") for k in ("resort", "state_prov", "country", "tier")},
                         "handle": r["instagram"], **a})

    print(f"Checked {checked} accounts ({len(watch)} on watchlist), {client.calls} API calls, "
          f"{len(new_hits)} new viral, {len(new_mega)} MEGA. {stop_reason or ''}")
    for m in new_mega:
        print(f"  MEGA   {m['resort']:<30} {m['value']:>10,} {m['metric']}  {m['permalink']}")
    for h in new_hits:
        print(f"  VIRAL  {h['resort']:<30} {h['value']:>10,} {h['metric']:<5} {h['reason']}  {h['permalink']}")

    if args.dry_run:
        return
    save_json(STATE, state)
    append_csv(VIRAL_LOG, new_hits, outputs.VIRAL_COLUMNS)
    with open(ACCOUNTS_OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=outputs.ACCOUNT_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(accounts)
    try:
        outputs.push_to_sheet(new_hits, accounts)
    except Exception as e:
        print("Sheet update failed:", e)
    try:
        outputs.email_mega(new_mega)
    except Exception as e:
        print("Mega email failed:", e)


if __name__ == "__main__":
    main()
