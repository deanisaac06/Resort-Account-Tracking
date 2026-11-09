"""Fill in and verify Instagram handles in resorts.csv.

  python find_handles.py --scrape   look for instagram.com links on each resort website
                                    (only rows with a website and no handle yet)
  python find_handles.py --guess    try likely handles via the API, keep one only if the
                                    Instagram profile's website matches the resort's domain
  python find_handles.py --verify   look up every handle via the API and record followers
                                    and whether the profile's website matches

Run it from GitHub Actions (workflow "Find handles") or locally. Review the changes
in resorts.csv afterwards - anything marked 'guess' is worth a quick look.
"""
import argparse
import csv
import os
import re
import time
from difflib import SequenceMatcher
from urllib.parse import urlparse

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
RESORTS = os.path.join(HERE, "resorts.csv")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
IG_LINK = re.compile(r"instagram\.com/([A-Za-z0-9_.]{2,30})", re.I)
NOT_HANDLES = {"p", "reel", "reels", "explore", "stories", "tv", "accounts", "about", "legal",
               "developer", "direct", "web", "instagram", "share", "embed.js", "static"}
STOPWORDS = {"ski", "resort", "mountain", "mtn", "area", "the", "and", "club", "hill", "valley",
             "basin", "peak", "snow", "park", "of", "at", "station", "centre", "center", "de", "du"}


def domain(url):
    if not url:
        return ""
    host = urlparse(url if "//" in url else "https://" + url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def load():
    with open(RESORTS, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return rows, list(rows[0].keys())


def save(rows, cols):
    for extra in ("ig_followers", "ig_website_match"):
        if extra not in cols:
            cols.append(extra)
    with open(RESORTS, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def pick_best(cands, row):
    cands = [c.lower().rstrip(".") for c in cands if c.lower() not in NOT_HANDLES]
    if not cands:
        return None
    ref = (domain(row["website"]).split(".")[0] + " " + re.sub(r"[^a-z0-9]", "", row["resort"].lower()))
    return max(dict.fromkeys(cands), key=lambda c: (SequenceMatcher(None, c, ref).ratio(), cands.count(c)))


def scrape(rows):
    s = requests.Session()
    s.headers["User-Agent"] = UA
    for r in rows:
        if r["instagram"] or not r["website"]:
            continue
        found = []
        for path in ("", "/contact", "/contact-us"):
            try:
                html = s.get(r["website"].rstrip("/") + path, timeout=20).text
            except requests.RequestException:
                continue
            found += IG_LINK.findall(html)
            if found:
                break
        best = pick_best(found, r)
        if best:
            r["instagram"], r["instagram_url"] = best, f"https://www.instagram.com/{best}/"
            r["ig_source"], r["track"] = "website (auto)", "yes"
            print(f"found  {r['resort']:<40} @{best}")
        else:
            r["ig_source"] = "not on website"
        time.sleep(0.5)


def candidates(row):
    d = domain(row["website"]).split(".")[0].replace("-", "")
    words = [w for w in re.sub(r"\(.*?\)", "", row["resort"].lower()).replace("'", "").split()]
    full = re.sub(r"[^a-z0-9]", "", "".join(words))
    core = "".join(w for w in words if w not in STOPWORDS)
    core = re.sub(r"[^a-z0-9]", "", core)
    c = [d, full, core, core + "resort", core + "mtn", core + "mtnresort", core + "mountain", "ski" + core, core + "skiarea"]
    return [x for x in dict.fromkeys(c) if 3 <= len(x) <= 30]


def guess(rows, client):
    from igapi import AccountUnavailable, RateLimited
    for r in rows:
        if r["instagram"] or not r["website"]:
            continue
        want = domain(r["website"])
        for cand in candidates(r):
            try:
                bd = client.business_discovery(cand, media_limit=1)
            except AccountUnavailable:
                continue
            except RateLimited:
                print("Rate limited - run again in an hour to continue")
                return
            if want and want.split("/")[0] in domain(bd.get("website", "")):
                r["instagram"], r["instagram_url"] = cand, f"https://www.instagram.com/{cand}/"
                r["ig_source"], r["track"] = "guess (website matches)", "yes"
                r["ig_followers"], r["ig_website_match"] = bd.get("followers_count", ""), "yes"
                print(f"guess  {r['resort']:<40} @{cand}")
                break
            time.sleep(0.4)


def verify(rows, client):
    from igapi import AccountUnavailable, RateLimited
    for r in rows:
        if not r["instagram"] or r.get("ig_followers"):
            continue
        try:
            bd = client.business_discovery(r["instagram"], media_limit=1)
        except AccountUnavailable:
            r["ig_website_match"], r["track"] = "NOT FOUND / personal account", ""
            print(f"missing {r['resort']:<40} @{r['instagram']}")
            continue
        except RateLimited:
            print("Rate limited - run again in an hour to continue")
            return
        r["ig_followers"] = bd.get("followers_count", "")
        want = domain(r["website"])
        r["ig_website_match"] = "yes" if want and want.split("/")[0] in domain(bd.get("website", "")) else "check"
        time.sleep(0.4)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scrape", action="store_true")
    ap.add_argument("--guess", action="store_true")
    ap.add_argument("--verify", action="store_true")
    a = ap.parse_args()
    rows, cols = load()
    for r in rows:
        r.setdefault("ig_followers", "")
        r.setdefault("ig_website_match", "")
    try:
        if a.scrape:
            scrape(rows)
        if a.guess or a.verify:
            from igapi import IGClient
            client = IGClient()
            if a.verify:
                verify(rows, client)
            if a.guess:
                guess(rows, client)
    finally:
        save(rows, cols)
    print("with handle:", sum(1 for r in rows if r["instagram"]), "of", len(rows))


if __name__ == "__main__":
    main()
