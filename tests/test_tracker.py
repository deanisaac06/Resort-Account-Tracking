import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import detect  # noqa: E402

NOW = datetime(2026, 11, 8, 20, 0, tzinfo=timezone.utc)


def ts(days_ago):
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S+0000")


def post(i, views=None, likes=None, days=1):
    return {"id": str(i), "permalink": f"https://www.instagram.com/reel/{i}/", "timestamp": ts(days),
            "view_count": views, "like_count": likes, "comments_count": 5, "media_type": "VIDEO",
            "caption": f"post {i}"}


def test_flags_outlier_against_account_median():
    posts = [post(1, views=400_000, likes=9000)] + [post(i, views=20_000, likes=500, days=i) for i in range(2, 10)]
    hits = detect.find_viral(posts, followers=50_000, now=NOW)
    assert [h["media_id"] for h in hits] == ["1"]
    assert hits[0]["multiple"] == 20.0


def test_normal_posts_not_flagged():
    posts = [post(i, views=20_000 + i * 1000, likes=500, days=i) for i in range(1, 12)]
    assert detect.find_viral(posts, followers=50_000, now=NOW) == []


def test_small_account_needs_floor():
    # 10x the median but only 3,000 views - below min_views floor, ignore
    posts = [post(1, views=3_000)] + [post(i, views=300, days=i) for i in range(2, 10)]
    assert detect.find_viral(posts, followers=2_000, now=NOW) == []


def test_small_account_real_breakout():
    posts = [post(1, views=120_000)] + [post(i, views=1_500, days=i) for i in range(2, 10)]
    hits = detect.find_viral(posts, followers=4_000, now=NOW)
    assert len(hits) == 1 and "views per follower" in hits[0]["reason"]


def test_absolute_threshold_and_age_limit():
    posts = [post(1, views=900_000, days=40), post(2, views=1_200_000, days=2)] + \
            [post(i, views=600_000, days=i) for i in range(3, 8)]
    hits = detect.find_viral(posts, followers=1_000_000, now=NOW)
    assert [h["media_id"] for h in hits] == ["2"]  # old post skipped, big account still flagged on absolute


def test_image_posts_use_likes():
    posts = [post(1, likes=30_000)] + [post(i, likes=800, days=i) for i in range(2, 9)]
    hits = detect.find_viral(posts, now=NOW)
    assert hits and hits[0]["metric"] == "likes"


def test_track_run_end_to_end(monkeypatch=None):
    """Run track.main() against a fake API and check state, CSV and dedupe."""
    import importlib
    tmp = tempfile.mkdtemp()
    src = os.path.dirname(HERE)
    for f in ("track.py", "detect.py", "igapi.py", "outputs.py", "settings.json"):
        shutil.copy(os.path.join(src, f), tmp)
    with open(os.path.join(tmp, "resorts.csv"), "w") as f:
        f.write("resort,state_prov,country,tier,instagram,track\n"
                "Big Hill,WA,USA,Major,bighill,yes\nTiny Hill,VT,USA,Small,tinyhill,yes\n"
                "Gone Hill,MT,USA,Small,gonehill,yes\nNo IG,ID,USA,Small,,\n")
    sys.path.insert(0, tmp)
    for m in ("track", "detect", "igapi", "outputs"):
        sys.modules.pop(m, None)
    os.environ.update(IG_USER_ID="1", IG_ACCESS_TOKEN="x")
    track = importlib.import_module("track")
    igapi = sys.modules["igapi"]
    recent = datetime.now(timezone.utc)

    def fake_bd(self, username, media_limit=12):
        self.calls += 1
        if username == "gonehill":
            raise igapi.AccountUnavailable("Cannot find User")
        base = 50_000 if username == "bighill" else 1_000
        media = [{"id": f"{username}{i}", "permalink": f"https://ig/{username}{i}",
                  "timestamp": (recent - timedelta(days=i)).strftime("%Y-%m-%dT%H:%M:%S+0000"),
                  "view_count": base, "like_count": base // 20, "comments_count": 3, "caption": "x"}
                 for i in range(1, 10)]
        if username == "tinyhill":
            media[0]["view_count"] = 250_000
        return {"followers_count": 5000, "media_count": 300, "media": {"data": media}}

    igapi.IGClient.business_discovery = fake_bd
    igapi.IGClient.polite_sleep = lambda self, s=0: None
    sys.argv = ["track.py"]
    track.main()
    state = json.load(open(os.path.join(tmp, "data", "state.json")))
    assert list(state["alerted"]) == ["tinyhill1"]
    assert state["accounts"]["gonehill"]["status"].startswith("unavailable")
    assert state["accounts"]["bighill"]["median_views"] == 50_000
    log = open(os.path.join(tmp, "data", "viral_posts.csv")).read()
    assert "Tiny Hill" in log and "250000" in log
    # second run must not re-alert the same post
    track.main()
    log2 = open(os.path.join(tmp, "data", "viral_posts.csv")).read()
    assert log2.count("tinyhill1") == 1
    accounts = open(os.path.join(tmp, "data", "accounts.csv")).read()
    assert "Big Hill" in accounts and "No IG" not in accounts


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)


def test_mega_rules():
    base = [post(i, views=30_000, days=i) for i in range(2, 10)]
    summit = detect.score_posts([post(1, views=3_500_000)] + base, followers=87_000, now=NOW)
    assert detect.is_mega(summit[0])                        # 3.5M, ~117x normal
    big_normal = detect.score_posts([post(1, views=2_500_000)] + [post(i, views=600_000, days=i) for i in range(2, 10)], now=NOW)
    assert not detect.is_mega(big_normal[0])                # 2.5M but only ~4x a big account's normal
    huge = detect.score_posts([post(1, views=6_000_000)] + [post(i, views=1_500_000, days=i) for i in range(2, 10)], now=NOW)
    assert detect.is_mega(huge[0])                          # 5M+ always counts
    modest = detect.score_posts([post(1, views=900_000)] + base, now=NOW)
    assert not detect.is_mega(modest[0]) and detect.is_watch(modest[0])  # climbing -> watchlist


def test_mega_email_and_watchlist_end_to_end():
    import importlib
    tmp = tempfile.mkdtemp()
    src = os.path.dirname(HERE)
    for f in ("track.py", "detect.py", "igapi.py", "outputs.py", "settings.json", "weekly.py"):
        shutil.copy(os.path.join(src, f), tmp)
    with open(os.path.join(tmp, "resorts.csv"), "w") as f:
        f.write("resort,state_prov,country,tier,instagram,track\n"
                + "".join(f"Hill {i},WA,USA,Small,hill{i},yes\n" for i in range(6))
                + "Summit,WA,USA,Major,summit,yes\n")
    sys.path.insert(0, tmp)
    for m in ("track", "detect", "igapi", "outputs", "weekly"):
        sys.modules.pop(m, None)
    os.environ.update(IG_USER_ID="1", IG_ACCESS_TOKEN="x")
    track = importlib.import_module("track")
    igapi, outputs = sys.modules["igapi"], sys.modules["outputs"]
    recent = datetime.now(timezone.utc)
    big = {"views": 400_000}
    calls = []

    def fake_bd(self, username, media_limit=12):
        self.calls += 1
        calls.append(username)
        media = [{"id": f"{username}{i}", "permalink": f"https://ig/{username}{i}",
                  "timestamp": (recent - timedelta(days=i)).strftime("%Y-%m-%dT%H:%M:%S+0000"),
                  "view_count": 30_000, "like_count": 900, "comments_count": 3, "caption": "pow day"}
                 for i in range(1, 10)]
        if username == "summit":
            media[0]["view_count"] = big["views"]
        return {"followers_count": 87_000, "media_count": 300, "media": {"data": media}}

    sent = []
    igapi.IGClient.business_discovery = fake_bd
    igapi.IGClient.polite_sleep = lambda self, s=0: None
    outputs.send_email = lambda subject, body, html=None, to=None: sent.append(subject) or True
    sys.argv = ["track.py", "--max-calls", "7"]
    track.main()                                   # 400K: climbing, not mega
    assert not sent
    big["views"] = 3_500_000
    sys.argv = ["track.py", "--max-calls", "2"]
    calls.clear()
    track.main()                                   # summit is on the watchlist -> checked first
    assert calls[0] == "summit"
    assert len(sent) == 1 and "MEGA VIRAL: Summit" in sent[0] and "3.5M views" in sent[0]
    track.main()                                   # never alerts twice
    assert len(sent) == 1
    weekly = importlib.import_module("weekly")
    state = json.load(open(os.path.join(tmp, "data", "state.json")))
    outp, biggest, megas, stats, s = weekly.build(state, {}, datetime.now(timezone.utc))
    assert megas[0]["handle"] == "summit" and megas[0]["value"] == 3_500_000
    assert all(p["handle"] != "summit" or p["value"] < 1_000_000 for p in outp + biggest)  # not repeated
    title, text, html = weekly.render(outp, biggest, megas, stats, s, datetime.now(timezone.utc))
    assert "MEGA VIRAL THIS WEEK" in text and "<table" in html


def test_weekly_history_note_and_render():
    import weekly
    hist = [post(i, views=20_000, days=i * 4) for i in range(1, 40)]          # ~5 months of normal reels
    hist += [post(99, views=150_000, days=150)]                              # one old hit
    p = {"media_id": "new", "timestamp": ts(0.5), "metric": "views", "value": 238_000}
    ctx = weekly.history_note(p, hist)
    assert ctx["note"].startswith("Best of its last 40 reels")
    p["value"] = 100_000
    assert weekly.history_note(p, hist)["note"] == "Best since " + (NOW - timedelta(days=150)).strftime("%b %Y")
    card = dict(media_id="1", permalink="https://ig/1", posted="2026-11-06", resort="Powder Mountain",
                handle="powdermountain", state_prov="UT", tier="Mid", metric="views", value=238_000,
                baseline=17_000, multiple=14.0, history=ctx, caption="lift test")
    stats = {"since": "2026-11-02", "posts_seen": 1, "accounts_checked": 1, "accounts_posting": 1, "unavailable": 0}
    s = dict(weekly.DEFAULTS)
    title, text, html = weekly.render([card], [], [], stats, s, NOW, "Para one.\nPara two.", {"1": "cid:p1"})
    assert "cid:p1" in html and "Para two." in html and "This week" in html and "14&times;" in html
    assert "Powder Mountain" in weekly.ai_rows([card])
