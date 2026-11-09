"""Where results go: a Google Sheet (via a free service account) and an email digest.
Both are optional - if the secrets aren't set, the tracker just writes CSVs in data/."""
import json
import os
import smtplib
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape

VIRAL_COLUMNS = [
    "detected_at", "resort", "handle", "state_prov", "tier", "permalink", "posted",
    "media_type", "views", "likes", "comments", "baseline", "multiple", "followers",
    "reason", "caption",
]
ACCOUNT_COLUMNS = [
    "resort", "handle", "state_prov", "country", "tier", "followers", "posts_total",
    "median_views", "median_likes", "last_post", "last_checked", "status",
]


# ---------------------------------------------------------------- Google Sheets
def _sheet():
    sa = os.environ.get("GOOGLE_SA_JSON")
    sheet_id = os.environ.get("SHEET_ID")
    if not sa or not sheet_id:
        return None
    import gspread  # imported lazily so the tracker runs without it

    gc = gspread.service_account_from_dict(json.loads(sa))
    return gc.open_by_key(sheet_id)


def _tab(sh, title, columns):
    try:
        ws = sh.worksheet(title)
    except Exception:
        ws = sh.add_worksheet(title=title, rows=1000, cols=len(columns))
        ws.append_row(columns)
        ws.freeze(rows=1)
    return ws


def push_to_sheet(new_hits, accounts):
    sh = _sheet()
    if sh is None:
        return False
    if new_hits:
        ws = _tab(sh, "Viral posts", VIRAL_COLUMNS)
        ws.append_rows([[h.get(c, "") if h.get(c) is not None else "" for c in VIRAL_COLUMNS] for h in new_hits],
                       value_input_option="USER_ENTERED")
    ws = _tab(sh, "Accounts", ACCOUNT_COLUMNS)
    rows = [ACCOUNT_COLUMNS] + [[a.get(c, "") if a.get(c) is not None else "" for c in ACCOUNT_COLUMNS] for a in accounts]
    ws.clear()
    ws.update(rows, "A1", value_input_option="USER_ENTERED")
    return True


# ---------------------------------------------------------------- Email
def send_email(subject, body, html=None, to=None, images=None):
    """images: optional {content_id: jpeg_bytes}, referenced in the html as <img src="cid:content_id">."""
    user, pw = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASS")
    to = to or os.environ.get("ALERT_TO")
    if not (user and pw and to):
        print("Email not configured (SMTP_USER / SMTP_PASS / ALERT_TO) - skipping:", subject)
        return False
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(body, "plain", "utf-8"))
    if html:
        alt.attach(MIMEText(html, "html", "utf-8"))
    if images:
        msg = MIMEMultipart("related")
        msg.attach(alt)
        for cid, data in images.items():
            img = MIMEImage(data, "jpeg")
            img.add_header("Content-ID", f"<{cid}>")
            img.add_header("Content-Disposition", "inline", filename=f"{cid}.jpg")
            msg.attach(img)
    else:
        msg = alt
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    with smtplib.SMTP_SSL(host, 465, timeout=30) as s:
        s.login(user, pw)
        s.sendmail(user, [a.strip() for a in to.split(",")], msg.as_string())
    return True


def fmt(n):
    if n is None:
        return "-"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n / 1000:.0f}K"
    return f"{n:,}"


def email_mega(megas):
    """One email per mega-viral post, sent the moment it's detected."""
    sent = 0
    for m in sorted(megas, key=lambda m: -m["value"]):
        mult = f"{m['multiple']:.0f}x their normal" if m.get("multiple") else "no baseline yet"
        subject = f"MEGA VIRAL: {m['resort']} - {fmt(m['value'])} {m['metric']} ({mult})"
        text = (f"{m['resort']} (@{m['handle']}, {m['state_prov']}) has a post that's blowing up.\n\n"
                f"{m['metric'].title()}: {m['value']:,}\n"
                f"Account normal: {fmt(m.get('baseline'))} {m['metric']} per post ({mult})\n"
                f"Followers: {fmt(m.get('followers'))}\n"
                f"Likes / comments: {fmt(m.get('likes'))} / {fmt(m.get('comments'))}\n"
                f"Posted: {m['posted']} ({m['age_days']} days ago), type: {m.get('media_type') or '-'}\n\n"
                f"{m['permalink']}\n\nCaption: {m.get('caption') or '-'}\n")
        html = f"""<div style="font-family:Arial,sans-serif;max-width:560px">
<p style="font-size:13px;color:#b42318;font-weight:bold;letter-spacing:.5px">MEGA VIRAL POST</p>
<h2 style="margin:4px 0">{escape(m['resort'])} <span style="color:#666;font-weight:normal">@{escape(m['handle'])} &middot; {escape(m['state_prov'])}</span></h2>
<p style="font-size:34px;font-weight:bold;margin:12px 0 4px">{fmt(m['value'])} {m['metric']}</p>
<p style="margin:0 0 14px;color:#444">{mult} (normal: {fmt(m.get('baseline'))}) &middot; {fmt(m.get('followers'))} followers &middot; posted {m['posted']}</p>
<p><a href="{m['permalink']}" style="background:#1f3a5f;color:#fff;padding:9px 16px;border-radius:6px;text-decoration:none">Open the post</a></p>
<p style="color:#555;font-size:13px">{escape(m.get('caption') or '')}</p></div>"""
        sent += bool(send_email(subject, text, html))
    return sent
