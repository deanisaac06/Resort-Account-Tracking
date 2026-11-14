# Ski Resort Viral Post Tracker

Watches the Instagram accounts of every US and Canadian ski area and flags posts that are
taking off. It runs free on GitHub Actions, sends results to a Google Sheet, and emails
you when something pops.

**How it works:** Instagram's *Business Discovery* API lets your own Instagram professional
account read public stats for any other Business/Creator account, including follower count
and recent posts with likes, comments and **views**. Each hour the tracker checks about 150
accounts, picking up where it stopped last time, so it covers the whole list every 4–5 hours.

A post is flagged as **viral** when it is 21 days old or less and any of these is true:

| Rule | Default |
|---|---|
| Beats the account's own median by 5× **and** clears a floor | 25K views (or 2.5K likes for photos) |
| Huge in absolute terms (and at least 1.5× the account's median) | 500K views / 25K likes |
| Views ≥ 3× the account's followers (and ≥ 2× its median, ≥ 25K views) | catches small hills breaking out |

You can change every number in `settings.json`. Each post is only alerted once.

---

## Files

| File | What it is |
|---|---|
| `resorts.csv` | The master list: 731 ski areas with state/province, tier, website and Instagram handle. Set `track` to blank to skip a resort. |
| `track.py` | The hourly job |
| `find_handles.py` | Fills in missing handles from resort websites and checks them through the API |
| `settings.json` | Viral thresholds |
| `data/` | Written by the tracker: `viral_posts.csv` (every hit), `accounts.csv` (followers and median views per resort), `state.json` |

---

## Setup (about 45 minutes, all free)

### 1. Instagram + Facebook Page (the "reader" account)
1. Create a new Instagram account for the agency.
2. In Instagram, open **Settings → Account type and tools → Switch to professional account** and pick **Business** or **Creator**.
3. Create a Facebook Page for the agency (facebook.com/pages/create). Any name works.
4. Link the two: on the Page, open **Settings → Linked accounts → Instagram → Connect**.

### 2. Meta developer app
1. Go to developers.facebook.com → **My Apps → Create app**.
2. Choose the use case that includes **Instagram API with Facebook Login** (or "Other" → app type **Business**).
3. Leave it in **Development** mode. You don't need App Review because you're the only user.

### 3. Get your token and Instagram user ID
1. Open the **Graph API Explorer** (developers.facebook.com/tools/explorer) and select your app.
2. Add these permissions: `instagram_basic`, `instagram_manage_insights`, `pages_show_list`, `pages_read_engagement`, `business_management`. Then click **Generate Access Token** and approve.
3. Run `me/accounts?fields=name,access_token,instagram_business_account`. The `instagram_business_account.id` is your **IG_USER_ID**.
4. Make the token long-lived so it doesn't expire after an hour. Open:
   `https://graph.facebook.com/v27.0/oauth/access_token?grant_type=fb_exchange_token&client_id=APP_ID&client_secret=APP_SECRET&fb_exchange_token=SHORT_TOKEN`
   Then run `me/accounts` again with that long-lived token. The Page `access_token` it returns doesn't expire, so use it as **IG_ACCESS_TOKEN**. (A long-lived user token also works, but it lasts 60 days. If the token ever stops working, the tracker emails you.)
5. Test it in the Explorer:
   `IG_USER_ID?fields=business_discovery.username(summitatsnoqualmie){followers_count,media{like_count,view_count,permalink}}`

### 4. GitHub (runs it for free)
1. Create a **private** repo and upload everything in this folder, including the hidden `.github` folder.
2. Open **Settings → Secrets and variables → Actions** and add the secrets `IG_USER_ID` and `IG_ACCESS_TOKEN`.
3. Open the **Actions** tab, then run **Find handles** with mode `--scrape --verify`. It looks for handles for the roughly 380 resorts without one and checks every handle against the API. Then run it again with `--guess`.
4. **Track viral posts** then runs every hour on its own. You can also start it by hand with **Run workflow**.

> Free private repos get 2,000 Actions minutes a month. Hourly runs use about 1,500 of them. If you get close to the limit, change the cron in `.github/workflows/track.yml` to `17 */2 * * *` (every 2 hours), or make the repo public, which gives unlimited minutes.

### 5. Google Sheet output (optional)
1. Create a Google Sheet and copy its ID from the URL (`/d/<SHEET_ID>/edit`).
2. In console.cloud.google.com, create a project, enable the **Google Sheets API**, create a **Service account**, and download a **JSON key**.
3. Share the Sheet with the service account's email as an **Editor**.
4. Add the secrets `SHEET_ID` and `GOOGLE_SA_JSON` (paste the whole JSON file).
The tracker adds new hits to a **Viral posts** tab and rewrites an **Accounts** tab with followers and median views for every resort, which is handy for benchmarking.

### 6. Emails
There are two emails:

| Email | When | What |
|---|---|---|
| **Weekly report** | Mondays ~7am Pacific (sent by the hourly tracker) | A short AI-written strategy read on the week, a picture grid of the top 20 posts that beat their own account's normal by 3x+ (with history notes like "best since March"), any mega-viral posts, and the 10 biggest posts by views |
| **Mega viral alert** | Within about an hour of detection (hourly tracker) | One email per post that hits 2M+ views **and** 10x the account's normal, or 5M+ views no matter what (100K+ likes and 10x for photos) |

The mega thresholds are set to be rare: a few times a season across the whole industry. Posts that pass 300K views (or 8x normal) go on a watchlist and get re-checked every hour, so a post that's taking off is caught fast. Tune all of this under `mega` and `weekly` in `settings.json`.

Setup:
1. Turn on 2-step verification for a Gmail account, then create an **App password** (myaccount.google.com/apppasswords).
2. Add the secrets `SMTP_USER` (the Gmail address), `SMTP_PASS` (the app password) and `ALERT_TO` (where emails go; separate several addresses with commas). Optional: `WEEKLY_TO` if the weekly report should go to different people.
3. To test, open Actions → **Weekly report** → Run workflow. Each report is also saved in `reports/`.

The AI summary uses Google's Gemini free tier: create a key at aistudio.google.com/apikey and add it as the secret `GEMINI_API_KEY`. The Monday report is sent by the hourly tracker run at 15:00 UTC (that hour skips tracking so the report has Instagram quota for fresh numbers, preview images and ~40 posts of history per top account). If the AI summary fails, the report is held and retried every hour; after 24 hours it goes out without the summary and with a warning at the top. Actions → Weekly report → Run workflow sends a copy by hand (tick "dry run" to test without emailing).

**Hourly runs:** GitHub's built-in schedule can be late or skipped, so each tracker run also has a `next-run` job that waits for the next hh:23 and starts the following run itself (free on a public repo). If the chain ever stops, run Track viral posts once by hand to restart it.

### 7. Monthly history (kept forever)
`data/history/monthly.csv` gets one row per resort per month, added on the 3rd of each month for the month before (by the hourly run, via `rollup.py`). Columns: followers and change, posts and reels, median and total reel views, median photo likes, **posts_3x** (posts that beat the account's own normal by 3x+), viral and mega counts, and the month's best post. That's about 500 rows a month (~1 MB a year), so it stays small while building up each resort's normal, how often it breaks out, and seasonality over time. To redo a month by hand: `python rollup.py 2026-11`.

**Backfill:** `backfill.py` pulls each account's last 200 posts once (50 per call, picking up where it left off each hour) and writes monthly rows for the past months they cover, so history starts well before tracking did. Backfilled months have no follower counts. Normal tracking is paused while it runs and resumes automatically when `data/backfill/progress.json` shows `completed`. `python backfill.py --status` shows progress.


**Deeper history:** `extend_history.py` keeps paging Major-tier accounts back to January 2021, since the first backfill only reached 2023-2024 for busy resorts. It runs after the tracker each hour and only uses spare quota (it stops at 60% of Meta's hourly limit, while the tracker stops at 85%), so tracking and alerts carry on as normal. It only adds months older than what `monthly.csv` already has, and it stops for good once every account is done. Progress is saved in `data/backfill/extend.json`, and `python extend_history.py --status` shows it. To go further, change `since` or add `"Mid"` to `tiers` under `history_extend` in settings.json, and it will pick up the new accounts.

---

## Running locally
```bash
pip install -r requirements.txt
export IG_USER_ID=... IG_ACCESS_TOKEN=...
python track.py --only summitatsnoqualmie --dry-run   # test one account
python track.py --dry-run                              # one batch, no writes
python -m pytest tests                                 # logic tests (no API needed)
```

## Limits to know
- Only **Business/Creator** accounts can be read. Nearly every resort qualifies; those that don't show `unavailable` in `accounts.csv`.
- The API doesn't give shares, saves or Stories. Views include both paid and organic plays, so a boosted Reel can trip the detector.
- Facebook Pages need Meta's "Page Public Content Access" feature, which requires App Review. TikTok has no free API for this. `resorts.csv` has empty `facebook`, `tiktok` and `youtube` columns ready for later.
