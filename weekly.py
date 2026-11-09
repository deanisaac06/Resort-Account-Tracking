"""Weekly performance report: a strategist's read on the week, a visual grid of the posts
that most outperformed their own account's normal, and the biggest posts overall.

Run:  python weekly.py                       (send the email + save reports/weekly-YYYY-MM-DD.md)
      python weekly.py --dry-run             (print the report, send nothing)
      python weekly.py --preview out.html    (also write a browser preview with images inlined)

It reads data/state.json, which the hourly tracker keeps filled with every recent post. Then it
re-checks the top accounts on Instagram (fresh numbers, preview images and ~40 posts of history
for context), and asks an AI model (free GitHub Models, using the workflow's GITHUB_TOKEN) to
write the summary. Every one of those extras is optional: if a step fails, the email still goes
out without it.
"""
import argparse
import base64
import io
import json
import os
from datetime import datetime, timedelta, timezone
from html import escape
from statistics import median

import requests

import outputs
from outputs import fmt

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "data", "state.json")
SETTINGS = os.path.join(HERE, "settings.json")
REPORTS = os.path.join(HERE, "reports")

DEFAULTS = {
    "lookback_days": 7,
    "min_multiple": 3.0,     # "outperformed" = at least 3x the account's normal...
    "min_views": 10_000,     # ...with at least this many views
    "min_likes": 1_000,      # ...or this many likes for photo posts
    "top_outperformers": 20,
    "top_biggest": 10,
    "history_posts": 40,     # posts of history pulled per top account for context
    "ai_summary": True,
    "ai_models": ["openai/gpt-4.1", "openai/gpt-4o"],
}

AI_URL = "https://models.github.ai/inference/chat/completions"
AI_SYSTEM = ("You are a senior social media strategist at a marketing agency that works with ski resorts. "
             "You write tight, specific analysis for a weekly industry email. No hype, no emojis, no bullet "
             "points, no headings. Only state facts that are in the data you are given.")
AI_PROMPT = """Below are this week's top Instagram posts from US and Canadian ski resorts, ranked by how far each beat its own account's normal (median of its recent posts). Reels are measured in views, photo/carousel posts in likes.

Write ONE or TWO short paragraphs (120-170 words total) that give an industry-wide, strategic read of what is working right now and why. Explain the patterns (formats, themes, timing in the season, hooks, behind-the-scenes vs. hype, etc.), and call out 2-4 specific standouts by name with concrete numbers. Where the history notes show something notable (an account's best post in months, or a small account punching far above its size), say so. Do not just list posts. Plain text only.

Week: {since} to {today}. {posts_seen} posts from {accounts_posting} resorts were tracked this week; {reel_share}% of all tracked posts were reels vs {top_reel_share}% of the top {n_top}.

{rows}"""


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default


# ------------------------------------------------------------------ pick the posts
def build(state, settings, now):
    s = dict(DEFAULTS, **settings.get("weekly", {}))
    since = (now - timedelta(days=s["lookback_days"])).strftime("%Y-%m-%d")
    week_all = [p for p in state.get("posts", {}).values() if p.get("posted", "") >= since]

    def clears_floor(p):
        return p["value"] >= (s["min_views"] if p["metric"] == "views" else s["min_likes"])

    megas = sorted((p for p in week_all if p.get("mega_alerted")), key=lambda p: -p["value"])
    week = [p for p in week_all if not p.get("mega_alerted")]   # megas get their own section
    outperformers = sorted(
        (p for p in week if (p.get("multiple") or 0) >= s["min_multiple"] and clears_floor(p)),
        key=lambda p: -(p["multiple"] or 0))[: s["top_outperformers"]]
    biggest = sorted((p for p in week if p["metric"] == "views"), key=lambda p: -p["value"])[: s["top_biggest"]]

    accounts = state.get("accounts", {})
    checked = [a for a in accounts.values() if a.get("last_checked", "") >= since]
    stats = {
        "since": since,
        "posts_seen": len(week_all),
        "accounts_checked": len(checked),
        "accounts_posting": len({p["handle"] for p in week_all}),
        "unavailable": sum(1 for a in accounts.values() if str(a.get("status", "")).startswith("unavailable")),
        "reel_share": round(100 * sum(p["metric"] == "views" for p in week_all) / max(len(week_all), 1)),
    }
    return outperformers, biggest, megas, stats, s


# ------------------------------------------------------------------ history + fresh numbers
def _ts(t):
    return datetime.strptime(t, "%Y-%m-%dT%H:%M:%S%z")


def _span(days):
    return f"{round(days / 30)} months" if days >= 60 else f"{days} days"


def history_note(p, hist):
    """How this post compares with the account's longer history, e.g. 'best post in 5 months'."""
    from detect import metric_for
    same = [h for h in hist if h.get("id") != p["media_id"] and h.get("timestamp") and metric_for(h)[0] == p["metric"]]
    if len(same) < 5 or not p.get("timestamp"):
        return None
    pts = _ts(p["timestamp"])
    older = [h for h in same if _ts(h["timestamp"]) < pts]
    ctx = {"long_normal": round(median(metric_for(h)[1] for h in same)), "history_n": len(same)}
    if len(older) >= 5:
        span_days = (pts - min(_ts(h["timestamp"]) for h in older)).days
        beat = [h for h in older if metric_for(h)[1] >= p["value"]]
        if not beat:
            ctx["note"] = f"Best of its last {len(older)} {'reels' if p['metric'] == 'views' else 'posts'} ({_span(span_days)})"
        else:
            last = max(_ts(h["timestamp"]) for h in beat)
            gap = (pts - last).days
            if gap >= 45:
                ctx["note"] = f"Best since {last.strftime('%b %Y')}"
    newer = [h for h in same if _ts(h["timestamp"]) >= pts]
    ctx["rank"] = 1 + sum(metric_for(h)[1] > p["value"] for h in same)
    ctx["posts_last_30d"] = sum((pts - _ts(h["timestamp"])).days <= 30 for h in older) + len(newer)
    return ctx


def enrich(posts, settings_s):
    """Re-check each account behind the top posts: fresh counts, preview image, and history context."""
    if not posts or not (os.environ.get("IG_USER_ID") and os.environ.get("IG_ACCESS_TOKEN")):
        print("No Instagram credentials - using stored numbers, no history context.")
        return
    from detect import metric_for
    from igapi import IGClient, AccountUnavailable, RateLimited, TokenError
    client = IGClient(usage_ceiling=95)
    handles = list(dict.fromkeys(p["handle"] for p in posts))
    hist_by = {}
    for h in handles:
        try:
            data = client.business_discovery(h, media_limit=settings_s["history_posts"])
        except (AccountUnavailable, RateLimited, TokenError, Exception) as e:  # noqa: B014
            print(f"  history skipped for @{h}: {str(e)[:80]}")
            if isinstance(e, (RateLimited, TokenError)):
                break
            continue
        hist_by[h] = (data.get("media") or {}).get("data", [])
        if client.stop:
            print("  near Meta's quota - stopping history lookups here")
            break
    for p in posts:
        hist = hist_by.get(p["handle"])
        if not hist:
            continue
        fresh = next((m for m in hist if m.get("id") == p["media_id"]), None)
        if fresh:
            name, value = metric_for(fresh)
            if name == p["metric"] and value:
                p["value"] = value
                if p.get("baseline"):
                    p["multiple"] = round(value / max(p["baseline"], 1), 1)
            p.update(views=fresh.get("view_count", p.get("views")), likes=fresh.get("like_count", p.get("likes")),
                     comments=fresh.get("comments_count", p.get("comments")))
            p["thumb"] = fresh.get("thumbnail_url") or fresh.get("media_url") or p.get("thumb")
        ctx = history_note(p, hist)
        if ctx:
            p["history"] = ctx
    print(f"  history pulled for {len(hist_by)}/{len(handles)} accounts ({client.calls} API calls, usage {client.last_usage})")


# ------------------------------------------------------------------ preview images
def fetch_thumb(url, w=300, h=375):
    """Download a post preview and crop it to a 4:5 card. Returns JPEG bytes or None."""
    if not url:
        return None
    try:
        from PIL import Image
        r = requests.get(url, timeout=20)
        if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image"):
            return None
        im = Image.open(io.BytesIO(r.content)).convert("RGB")
        target = w / h
        iw, ih = im.size
        if iw / ih > target:      # too wide -> trim the sides
            nw = int(ih * target)
            im = im.crop(((iw - nw) // 2, 0, (iw - nw) // 2 + nw, ih))
        else:                     # too tall (reels are 9:16) -> trim top and bottom
            nh = int(iw / target)
            im = im.crop((0, (ih - nh) // 2, iw, (ih - nh) // 2 + nh))
        im = im.resize((w, h), Image.LANCZOS)
        out = io.BytesIO()
        im.save(out, "JPEG", quality=72, optimize=True, progressive=True)
        return out.getvalue()
    except Exception as e:
        print("  preview failed:", str(e)[:80])
        return None


# ------------------------------------------------------------------ AI summary
def ai_rows(posts):
    rows = []
    for i, p in enumerate(posts, 1):
        h = p.get("history") or {}
        bits = [f"{i}. {p['resort']} (@{p['handle']}, {p['state_prov']}, {p.get('tier') or '?'} resort, "
                f"{fmt(p.get('followers'))} followers)",
                f"{'REEL' if p['metric'] == 'views' else (p.get('media_type') or 'POST')}, posted {p['posted']}",
                f"{p['value']:,} {p['metric']} = {p['multiple']}x its normal of {fmt(p.get('baseline'))}"
                if p.get("multiple") else f"{p['value']:,} {p['metric']}",
                f"{fmt(p.get('likes'))} likes, {fmt(p.get('comments'))} comments"]
        if p.get("mega_alerted"):
            bits.append("MEGA VIRAL")
        if h.get("note"):
            bits.append(h["note"])
        if h.get("posts_last_30d") is not None:
            bits.append(f"account posted {h['posts_last_30d']}x in the 30 days around it")
        rows.append(" | ".join(bits) + f"\n   Caption: {(p.get('caption') or '-')[:180]}")
    return "\n".join(rows)


def ai_summary(posts, stats, s, now):
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GITHUB_MODELS_TOKEN")
    if not (s.get("ai_summary") and token and posts):
        print("AI summary skipped (no GITHUB_TOKEN, turned off, or no posts).")
        return None
    top_reels = round(100 * sum(p["metric"] == "views" for p in posts) / len(posts))
    prompt = AI_PROMPT.format(since=stats["since"], today=now.strftime("%Y-%m-%d"), posts_seen=stats["posts_seen"],
                              accounts_posting=stats["accounts_posting"], reel_share=stats["reel_share"],
                              top_reel_share=top_reels, n_top=len(posts), rows=ai_rows(posts))
    for model in s["ai_models"]:
        try:
            r = requests.post(AI_URL, timeout=90, headers={
                "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"},
                json={"model": model, "temperature": 0.4, "max_tokens": 450,
                      "messages": [{"role": "system", "content": AI_SYSTEM}, {"role": "user", "content": prompt}]})
            if r.status_code == 200:
                text = r.json()["choices"][0]["message"]["content"].strip()
                print(f"AI summary from {model}: {len(text)} chars")
                return text
            print(f"AI model {model} failed: {r.status_code} {r.text[:200]}")
        except Exception as e:
            print(f"AI model {model} error: {str(e)[:200]}")
    return None


# ------------------------------------------------------------------ rendering
def mult_str(m):
    return f"{m:.0f}" if m >= 10 else f"{m:.1f}"


def kind(p):
    t = (p.get("media_type") or "").upper()
    if p["metric"] == "views" or t in ("REELS", "VIDEO"):
        return "Reel"
    return "Carousel" if "CAROUSEL" in t else "Photo"


def row_text(i, p):
    mult = f"{mult_str(p['multiple'])}x normal" if p.get("multiple") else "no baseline"
    note = f" - {p['history']['note']}" if (p.get("history") or {}).get("note") else ""
    return (f"{i}. {p['resort']} (@{p['handle']}, {p['state_prov']}) - {fmt(p['value'])} {p['metric']}, "
            f"{mult} (normal {fmt(p.get('baseline'))}), posted {p['posted']}{note}\n   {p['permalink']}\n"
            f"   {(p.get('caption') or '')[:110]}")


def row_html(i, p):
    mult = f"{mult_str(p['multiple'])}&times;" if p.get("multiple") else "&ndash;"
    return (f"<tr><td style='padding:6px 8px;color:#888'>{i}</td>"
            f"<td style='padding:6px 8px'><b>{escape(p['resort'])}</b><br><span style='color:#777;font-size:12px'>@{escape(p['handle'])} &middot; {escape(p['state_prov'])} &middot; {escape(p.get('tier') or '')}</span></td>"
            f"<td style='padding:6px 8px;text-align:right'><b>{fmt(p['value'])}</b> {p['metric']}</td>"
            f"<td style='padding:6px 8px;text-align:right'>{mult}<br><span style='color:#777;font-size:12px'>normal {fmt(p.get('baseline'))}</span></td>"
            f"<td style='padding:6px 8px'>{p['posted']}</td>"
            f"<td style='padding:6px 8px'><a href='{p['permalink']}'>View</a></td></tr>"
            f"<tr><td></td><td colspan='5' style='padding:0 8px 10px;color:#555;font-size:12px;border-bottom:1px solid #eee'>{escape((p.get('caption') or '')[:160])}</td></tr>")


def card_html(i, p, img_src, accent="#0a7d32"):
    posted = datetime.strptime(p["posted"], "%Y-%m-%d").strftime("%b %-d")
    mult = (f"<span style='color:{accent};font-weight:bold'>{mult_str(p['multiple'])}&times;</span>"
            if p.get("multiple") else "")
    if img_src:
        pic = (f"<img src='{img_src}' width='150' height='188' alt='{escape(p['resort'])}' "
               f"style='display:block;width:150px;height:188px;border-radius:8px;border:0;background:#eee'>")
    else:
        pic = (f"<div style='width:150px;height:188px;border-radius:8px;background:#e9edf2;color:#8a94a3;"
               f"font-size:12px;line-height:188px;text-align:center'>{kind(p)} &middot; open post</div>")
    note = (p.get("history") or {}).get("note")
    note_html = (f"<div style='font-size:11px;color:#b54708;font-weight:bold;margin-top:2px'>{escape(note)}</div>"
                 if note else "")
    return (f"<div style='display:inline-block;width:150px;margin:0 6px 18px;vertical-align:top;text-align:left'>"
            f"<a href='{p['permalink']}' style='text-decoration:none'>{pic}</a>"
            f"<div style='font-size:11px;color:#8a94a3;margin-top:6px'>#{i} &middot; {kind(p)} &middot; {escape(p['state_prov'])}</div>"
            f"<div style='font-size:13px;font-weight:bold;color:#111;line-height:1.25'>"
            f"<a href='{p['permalink']}' style='color:#111;text-decoration:none'>{escape(p['resort'])}</a></div>"
            f"<div style='font-size:13px;color:#111'><b>{fmt(p['value'])}</b> {p['metric']} &nbsp;{mult}</div>"
            f"<div style='font-size:11px;color:#8a94a3'>normal {fmt(p.get('baseline'))} &middot; {posted}</div>"
            f"{note_html}</div>")


def render(outperformers, biggest, megas, stats, s, now, summary=None, img_src=None):
    """img_src: {media_id: src} for preview images (cid: links for email, data: URIs for previews)."""
    img_src = img_src or {}
    title = f"Ski resort Instagram: week ending {now.strftime('%b %-d, %Y')}"
    head = (f"{stats['posts_seen']} posts from {stats['accounts_posting']} resorts in the last {s['lookback_days']} days "
            f"({stats['accounts_checked']} accounts checked). Outperformed = at least {s['min_multiple']:g}x that "
            f"account's own normal.")
    text = [title, "", head, ""]
    if summary:
        text += ["THIS WEEK", summary, ""]
    if megas:
        text += ["MEGA VIRAL THIS WEEK"] + [row_text(i, p) for i, p in enumerate(megas, 1)] + [""]
    text += ["TOP OUTPERFORMERS (vs their own normal)"]
    text += [row_text(i, p) for i, p in enumerate(outperformers, 1)] or ["None this week."]
    text += ["", "BIGGEST POSTS BY VIEWS"] + [row_text(i, p) for i, p in enumerate(biggest, 1)]

    table = ("<table style='border-collapse:collapse;width:100%;font-size:14px'>"
             "<tr style='text-align:left;color:#888;font-size:12px'><th></th><th style='padding:6px 8px'>Resort</th>"
             "<th style='padding:6px 8px;text-align:right'>Result</th><th style='padding:6px 8px;text-align:right'>vs normal</th>"
             "<th style='padding:6px 8px'>Posted</th><th></th></tr>{}</table>")
    h2 = "<h3 style='margin:28px 0 4px;font-size:17px;color:#111'>{}</h3>"
    sub = "<p style='margin:0 0 14px;color:#8a94a3;font-size:12px'>{}</p>"
    html = [f"<div style='font-family:-apple-system,Segoe UI,Arial,sans-serif;max-width:680px;margin:0 auto;color:#222'>"
            f"<h2 style='margin:0 0 4px;font-size:22px;color:#111'>{escape(title)}</h2>",
            f"<p style='color:#666;margin:0 0 6px;font-size:13px'>{escape(head)}</p>"]
    if summary:
        paras = "".join(f"<p style='margin:0 0 10px'>{escape(x.strip())}</p>" for x in summary.split("\n") if x.strip())
        html.append(h2.format("This week") +
                    f"<div style='font-size:15px;line-height:1.55;color:#222;background:#f6f8fa;border-radius:10px;"
                    f"padding:14px 16px 6px'>{paras}</div>")
    if megas:
        html.append(h2.format("<span style='color:#b42318'>Mega viral</span>") + sub.format("2M+ views and 10x normal, or 5M+")
                    + "<div style='margin:0 -6px'>"
                    + "".join(card_html(i, p, img_src.get(p["media_id"]), "#b42318") for i, p in enumerate(megas, 1))
                    + "</div>")
    html.append(h2.format(f"Top {len(outperformers)} outperformers") +
                sub.format("Ranked by how far each post beat its own account's normal. Tap a post to open it."))
    html.append("<div style='margin:0 -6px'>" + "".join(card_html(i, p, img_src.get(p["media_id"]))
                                                         for i, p in enumerate(outperformers, 1)) + "</div>"
                if outperformers else "<p>None this week.</p>")
    html.append(h2.format("Biggest posts by views") + table.format("".join(row_html(i, p) for i, p in enumerate(biggest, 1))))
    if stats["unavailable"]:
        html.append(f"<p style='color:#888;font-size:12px'>{stats['unavailable']} accounts couldn't be read "
                    f"(personal accounts or renamed handles) - see data/accounts.csv.</p>")
    html.append("</div>")
    return title, "\n".join(text), "".join(html)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--preview", help="write an HTML preview with images inlined to this path")
    args = ap.parse_args()
    now = datetime.now(timezone.utc)
    state, settings = load(STATE, {}), load(SETTINGS, {})
    outperformers, biggest, megas, stats, s = build(state, settings, now)

    top = megas + outperformers
    enrich(top, s)
    images = {}
    for p in top:
        data = fetch_thumb(p.get("thumb"))
        if data:
            images[f"p{p['media_id']}"] = data
    print(f"Preview images: {len(images)}/{len(top)}")
    summary = ai_summary(top, stats, s, now)

    cid = {p["media_id"]: f"cid:p{p['media_id']}" for p in top if f"p{p['media_id']}" in images}
    title, text, html = render(outperformers, biggest, megas, stats, s, now, summary, cid)
    print(text)
    if args.preview:
        uri = {p["media_id"]: "data:image/jpeg;base64," + base64.b64encode(images[f"p{p['media_id']}"]).decode()
               for p in top if f"p{p['media_id']}" in images}
        with open(args.preview, "w", encoding="utf-8") as f:
            f.write(render(outperformers, biggest, megas, stats, s, now, summary, uri)[2])
    if args.dry_run:
        return
    os.makedirs(REPORTS, exist_ok=True)
    with open(os.path.join(REPORTS, f"weekly-{now.strftime('%Y-%m-%d')}.md"), "w", encoding="utf-8") as f:
        f.write(text + "\n")
    to = os.environ.get("WEEKLY_TO") or os.environ.get("ALERT_TO")
    outputs.send_email(title, text, html, to=to, images=images)


if __name__ == "__main__":
    main()
