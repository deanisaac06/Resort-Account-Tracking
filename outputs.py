"""Where results go: a Google Sheet (via a free service account) and an email digest.
Both are optional - if the secrets aren't set, the tracker just writes CSVs in data/."""
import json
import os
import smtplib
from email.mime.text import MIMEText

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
def send_email(subject, body):
    user, pw, to = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASS"), os.environ.get("ALERT_TO")
    if not (user and pw and to):
        return False
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    with smtplib.SMTP_SSL(host, 465, timeout=30) as s:
        s.login(user, pw)
        s.sendmail(user, [a.strip() for a in to.split(",")], msg.as_string())
    return True


def email_digest(new_hits):
    if not new_hits:
        return False
    new_hits = sorted(new_hits, key=lambda h: -(h.get("value") or 0))
    lines = [f"{len(new_hits)} new viral post(s) from ski resort accounts:\n"]
    for h in new_hits:
        stat = f"{h['value']:,} {h['metric']}"
        lines.append(
            f"- {h['resort']} (@{h['handle']}, {h['state_prov']}): {stat}"
            + (f", {h['multiple']}x their median" if h.get("multiple") else "")
            + f"\n  {h['permalink']}\n  {h['caption'][:120]}\n"
        )
    top = new_hits[0]
    subject = f"Viral ski post: {top['resort']} - {top['value']:,} {top['metric']}" + (
        f" (+{len(new_hits) - 1} more)" if len(new_hits) > 1 else "")
    return send_email(subject, "\n".join(lines))
