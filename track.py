"""Check a rotating batch of resort Instagram accounts and flag viral posts.

Run:  python track.py            (normal run, used by GitHub Actions)
      python track.py --dry-run  (no sheet/email, prints hits)

Each run checks up to MAX_CALLS accounts (Business Discovery allows ~200 calls/hour
per app user), picking up where the last run stopped, so the whole list is covered
every few hours.
"""
import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone
from statistics import median

from detect import find_viral
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

    client = IGClient()
    now = datetime.now(timezone.utc)
    now_s = now.strftime("%Y-%m-%d %H:%M UTC")
    cursor = 0 if args.only else state.get("cursor", 0) % len(resorts)
    new_hits, checked, stop_reason = [], 0, None

    for i in range(min(args.max_calls, len(resorts))):
        r = resorts[(cursor + i) % len(resorts)]
        handle = r["instagram"].strip().lstrip("@").lower()
        acct = state["accounts"].setdefault(handle, {})
        try:
            data = client.business_discovery(handle, media_limit=settings.get("posts_per_account", 12))
        except AccountUnavailable as e:
            acct.update(status="unavailable: " + str(e)[:80], last_checked=now_s)
            checked += 1
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
            continue

        checked += 1
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
        for hit in find_viral(posts, followers, settings.get("viral", {}), now):
            if hit["media_id"] in state["alerted"]:
                continue
            hit.update(detected_at=now_s, resort=r["resort"], handle=handle,
                       state_prov=r.get("state_prov", ""), tier=r.get("tier", ""),
                       posted=hit["timestamp"][:10])
            state["alerted"][hit["media_id"]] = {"handle": handle, "at": now_s, "value": hit["value"]}
            new_hits.append(hit)
        client.polite_sleep(settings.get("sleep_seconds", 0.5))

    if not args.only:
        state["cursor"] = (cursor + checked) % len(resorts)
    state["last_run"] = {"at": now_s, "checked": checked, "api_calls": client.calls,
                         "new_hits": len(new_hits), "stopped": stop_reason, "usage": client.last_usage}

    # trim alert memory to the last ~60 days so state.json stays small
    if len(state["alerted"]) > 5000:
        for k in sorted(state["alerted"], key=lambda k: state["alerted"][k]["at"])[:-5000]:
            state["alerted"].pop(k)

    accounts = []
    for r in load_resorts():
        a = state["accounts"].get(r["instagram"].strip().lstrip("@").lower(), {})
        accounts.append({**{k: r.get(k, "") for k in ("resort", "state_prov", "country", "tier")},
                         "handle": r["instagram"], **a})

    print(f"Checked {checked} accounts, {client.calls} API calls, {len(new_hits)} new viral posts. {stop_reason or ''}")
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
        outputs.email_digest(new_hits)
    except Exception as e:
        print("Email failed:", e)


if __name__ == "__main__":
    main()
