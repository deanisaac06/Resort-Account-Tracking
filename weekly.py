"""Weekly performance report: the posts from the last 7 days that most outperformed
their own account's normal, plus the biggest posts overall.

Run:  python weekly.py            (send the email + save reports/weekly-YYYY-MM-DD.md)
      python weekly.py --dry-run  (print the report, send nothing)

It reads data/state.json, which the hourly tracker keeps filled with every recent post.
"""
import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from html import escape

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
}


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default


def build(state, settings, now):
    s = dict(DEFAULTS, **settings.get("weekly", {}))
    since = (now - timedelta(days=s["lookback_days"])).strftime("%Y-%m-%d")
    week = [p for p in state.get("posts", {}).values() if p.get("posted", "") >= since]

    def clears_floor(p):
        return p["value"] >= (s["min_views"] if p["metric"] == "views" else s["min_likes"])

    megas = sorted((p for p in week if p.get("mega_alerted")), key=lambda p: -p["value"])
    week = [p for p in week if not p.get("mega_alerted")]   # megas get their own section
    outperformers = sorted(
        (p for p in week if (p.get("multiple") or 0) >= s["min_multiple"] and clears_floor(p)),
        key=lambda p: -(p["multiple"] or 0))[: s["top_outperformers"]]
    biggest = sorted((p for p in week if p["metric"] == "views"), key=lambda p: -p["value"])[: s["top_biggest"]]

    accounts = state.get("accounts", {})
    checked = [a for a in accounts.values() if a.get("last_checked", "") >= since]
    stats = {
        "since": since,
        "posts_seen": len(week) + len(megas),
        "accounts_checked": len(checked),
        "accounts_posting": len({p["handle"] for p in week + megas}),
        "unavailable": sum(1 for a in accounts.values() if str(a.get("status", "")).startswith("unavailable")),
    }
    return outperformers, biggest, megas, stats, s


def mult_str(m):
    return f"{m:.0f}" if m >= 10 else f"{m:.1f}"


def row_text(i, p):
    mult = f"{mult_str(p['multiple'])}x normal" if p.get("multiple") else "no baseline"
    return (f"{i}. {p['resort']} (@{p['handle']}, {p['state_prov']}) - {fmt(p['value'])} {p['metric']}, "
            f"{mult} (normal {fmt(p.get('baseline'))}), posted {p['posted']}\n   {p['permalink']}\n"
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


def render(outperformers, biggest, megas, stats, s, now):
    title = f"Ski resort Instagram: week ending {now.strftime('%b %-d, %Y')}"
    head = (f"{stats['posts_seen']} posts from {stats['accounts_posting']} resorts in the last {s['lookback_days']} days "
            f"({stats['accounts_checked']} accounts checked). Outperformed = at least {s['min_multiple']:g}x that "
            f"account's own normal.")
    text = [title, "", head, ""]
    if megas:
        text += ["MEGA VIRAL THIS WEEK"] + [row_text(i, p) for i, p in enumerate(megas, 1)] + [""]
    text += ["TOP OUTPERFORMERS (vs their own normal)"]
    text += [row_text(i, p) for i, p in enumerate(outperformers, 1)] or ["None this week."]
    text += ["", "BIGGEST POSTS BY VIEWS"] + [row_text(i, p) for i, p in enumerate(biggest, 1)]

    table = ("<table style='border-collapse:collapse;width:100%;font-size:14px'>"
             "<tr style='text-align:left;color:#888;font-size:12px'><th></th><th style='padding:6px 8px'>Resort</th>"
             "<th style='padding:6px 8px;text-align:right'>Result</th><th style='padding:6px 8px;text-align:right'>vs normal</th>"
             "<th style='padding:6px 8px'>Posted</th><th></th></tr>{}</table>")
    html = [f"<div style='font-family:Arial,sans-serif;max-width:760px'><h2 style='margin-bottom:4px'>{escape(title)}</h2>",
            f"<p style='color:#555;margin-top:0'>{escape(head)}</p>"]
    if megas:
        html.append("<h3 style='color:#b42318'>Mega viral this week</h3>"
                    + table.format("".join(row_html(i, p) for i, p in enumerate(megas, 1))))
    html.append("<h3>Top outperformers <span style='font-weight:normal;color:#777;font-size:13px'>vs their own normal</span></h3>")
    html.append(table.format("".join(row_html(i, p) for i, p in enumerate(outperformers, 1)))
                if outperformers else "<p>None this week.</p>")
    html.append("<h3>Biggest posts by views</h3>" + table.format("".join(row_html(i, p) for i, p in enumerate(biggest, 1))))
    if stats["unavailable"]:
        html.append(f"<p style='color:#888;font-size:12px'>{stats['unavailable']} accounts couldn't be read "
                    f"(personal accounts or renamed handles) - see data/accounts.csv.</p>")
    html.append("</div>")
    return title, "\n".join(text), "".join(html)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    now = datetime.now(timezone.utc)
    state, settings = load(STATE, {}), load(SETTINGS, {})
    outperformers, biggest, megas, stats, s = build(state, settings, now)
    title, text, html = render(outperformers, biggest, megas, stats, s, now)
    print(text)
    if args.dry_run:
        return
    os.makedirs(REPORTS, exist_ok=True)
    with open(os.path.join(REPORTS, f"weekly-{now.strftime('%Y-%m-%d')}.md"), "w", encoding="utf-8") as f:
        f.write(text + "\n")
    to = os.environ.get("WEEKLY_TO") or os.environ.get("ALERT_TO")
    outputs.send_email(title, text, html, to=to)


if __name__ == "__main__":
    main()
